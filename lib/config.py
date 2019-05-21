import os
import time
import copy
import glob
import socket
import hashlib

import traceback

import yaml
import pykube
import urlparse
import requests
import netifaces

import avahi
import dbus
import encodings.idna


WPA = """ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=%s

network={
\tscan_ssid=1
\tssid="%s"
\t%s
}
"""

RESOURCES = [
    "Namespace",
    "ConfigMap",
    "Secret",
    "ServiceAccount",
    "ClusterRole",
    "ClusterRoleBinding",
    "Role",
    "RoleBinding"
]

SERVER = """server {

    listen       %s;
    server_name  %s;

    location / {
        proxy_pass %s://%s:%s/;
    }

}

"""


class AppException(Exception):
    pass

class Daemon(object):

    def __init__(self):

       self.config = {}
       self.mtimes = {}
       self.modified = []

       self.kube = None
       self.node = None
       self.cnames = set()

    def execute(self, command):

        print command
        os.system(command)

    def reset(self):

        print "reseting"

        self.execute("rm /boot/klot-io/reset")

        with open("/opt/klot-io/config/account.yaml", "w") as yaml_file:
            yaml.safe_dump({"password": "kloudofthings", "ssh": "disabled"}, yaml_file, default_flow_style=False)

        with open("/opt/klot-io/config/network.yaml", "w") as yaml_file:
            yaml.safe_dump({"interface": "eth0"}, yaml_file, default_flow_style=False)

        with open("/opt/klot-io/config/kubernetes.yaml", "w") as yaml_file:
            yaml.safe_dump({"role": "reset"}, yaml_file, default_flow_style=False)

    def restart(self):
        
        print "restarting"

        self.execute("cp /boot/klot-io/lib/config.py /opt/klot-io/lib/config.py")
        self.execute("chown 1000:1000 /opt/klot-io/lib/config.py")
        self.execute("chmod a+x /opt/klot-io/lib/config.py")

        self.execute("rm /boot/klot-io/lib/config.py")

        self.execute("systemctl restart klot-io-daemon")

    def reload(self):

        reloaded = False

        for yaml_path in glob.glob("/boot/klot-io/config/*.yaml"):
            self.execute("mv %s /opt/klot-io/config/" % yaml_path)
            reloaded = True

        if reloaded:
            self.execute("chown -R pi /opt/klot-io/config/")

    def load(self):

        self.modified = []

        for path in glob.glob("/opt/klot-io/config/*.yaml"):

            config = path.split("/")[-1].split('.')[0]
            mtime = os.path.getmtime(path)

            if config not in self.mtimes or self.mtimes[config] != mtime:

                with open(path, "r") as yaml_file:
                    self.config[config] = yaml.safe_load(yaml_file)

                self.mtimes[config] = mtime
                self.modified.append(config)

    def differs(self, expected, actual):

        print "actual:   %s" % actual
        print "expected: %s" % expected

        return expected != actual

    # Stolen from https://gist.github.com/gdamjan/3168336

    TTL = 15
    # Got these from /usr/include/avahi-common/defs.h
    CLASS_IN = 0x01
    TYPE_CNAME = 0x05

    @staticmethod
    def encode_cname(name):
        return '.'.join(  encodings.idna.ToASCII(p) for p in name.split('.') if p )

    @staticmethod
    def encode_rdata(name):
        def enc(part):
            a =  encodings.idna.ToASCII(part)
            return chr(len(a)), a
        return ''.join( '%s%s' % enc(p) for p in name.split('.') if p ) + '\0'

    def avahi(self):

        self.execute("systemctl restart avahi-daemon")

        if self.cnames:

            bus = dbus.SystemBus()
            server = dbus.Interface(bus.get_object(avahi.DBUS_NAME, avahi.DBUS_PATH_SERVER), avahi.DBUS_INTERFACE_SERVER)
            group = dbus.Interface(bus.get_object(avahi.DBUS_NAME, server.EntryGroupNew()), avahi.DBUS_INTERFACE_ENTRY_GROUP)

            for cname in self.cnames:
                group.AddRecord(
                    avahi.IF_UNSPEC,
                    avahi.PROTO_UNSPEC,
                    dbus.UInt32(0),
                    self.encode_cname(cname), 
                    self.CLASS_IN, 
                    self.TYPE_CNAME, 
                    self.TTL, 
                    avahi.string_to_byte_array(self.encode_rdata(server.GetHostNameFqdn()))
                )

            group.Commit()

    def account(self):

        self.execute("echo 'pi:%s' | chpasswd" % self.config["account"]["password"])

        if self.config["account"]["ssh"] == "enabled":
            self.execute("systemctl enable ssh")
            self.execute("systemctl start ssh")
        else:
            self.execute("systemctl stop ssh")
            self.execute("systemctl disable ssh")

    def network(self):

        if "network" not in self.modified:
            return

        expected = self.config["network"]['interface']

        with open("/etc/avahi/avahi-daemon.conf", "r") as avahi_file:
            for avahi_line in avahi_file:
                if "allow-interfaces" in avahi_line:
                    actual = avahi_line.split('=')[-1].strip()

        if self.differs(expected, actual):

            os.system("sed -i 's/allow-interfaces=.*/allow-interfaces=%s/' /etc/avahi/avahi-daemon.conf" % expected)
            self.avahi()

        if expected == "eth0":

            self.execute("sudo ifconfig wlan0 down")

            expected = WPA % ("NOPE", "nope", 'key_mgmt=NONE')

        elif expected == "wlan0":

            self.execute("sudo ifconfig wlan0 up")

            expected = WPA % (
                self.config["network"]["country"],
                self.config["network"]["ssid"],
                'psk="%s"' % self.config["network"]["psk"] if self.config["network"]["psk"] else 'key_mgmt=NONE'
            )

        with open("/etc/wpa_supplicant/wpa_supplicant.conf", "r") as wpa_file:
            actual = wpa_file.read()

        if self.differs(expected, actual):

            with open("/etc/wpa_supplicant/wpa_supplicant.conf", "w") as wpa_file:
                wpa_file.write(expected)

            self.execute("wpa_cli -i wlan0 reconfigure")

    def interfaces(self):

        interfaces = {}
        for interface in netifaces.interfaces():

            ifaddresses = netifaces.ifaddresses(interface)

            if netifaces.AF_INET in ifaddresses:
                interfaces[interface] = ifaddresses[netifaces.AF_INET][0]['addr']

        return interfaces

    def host(self, expected):

        self.node = expected

        avahi = False

        with open("/etc/hostname", "r") as hostname_file:
            actual = hostname_file.read()

        if self.differs(expected, actual):

            with open("/etc/hostname", "w") as hostname_file:
                hostname_file.write(expected)

            self.execute("hostnamectl set-hostname %s" % expected)

            avahi = True

        with open("/etc/hosts", "r") as hosts_file:
            actual = hosts_file.readlines()[-1].split("\t")[-1].strip()

        if self.differs(expected, actual):
            self.execute("sed -i 's/127.0.1.1\t.*/127.0.1.1\t%s/' /etc/hosts" % expected)
            avahi = True

        if avahi:
            self.avahi()

    def kubernetes(self):

        if self.config["kubernetes"]["role"] == "reset":

            if not os.path.exists("/home/pi/.kube/config"):
                print "already reset kubernetes"
                return

            try:
                pykube.Node.objects(self.kube).filter().get(name=self.node).delete()
            except pykube.ObjectDoesNotExist:
                pass

            self.host("klot-io")
            self.execute("rm -f /opt/klot-io/config/kubernetes.yaml")
            self.execute("rm -f /home/pi/.kube/config")
            self.execute("kubeadm reset")
            self.execute("reboot")
 
        attempts = 20

        while attempts:

            interfaces = self.interfaces()
            print "interfaces: %s" % interfaces

            if self.config["network"]['interface'] in interfaces:
                break

            time.sleep(5)
            attempts -= 1

        ip = interfaces[self.config["network"]['interface']]
        encoded = hashlib.sha256(self.config["account"]["password"]).hexdigest()
        token = "%s.%s" % (encoded[13:19], encoded[23:39])

        if self.config["kubernetes"]["role"] == "master":

            self.host("%s-klot-io" % self.config["kubernetes"]["cluster"])

            if os.path.exists("/home/pi/.kube/config"):
                print "already initialized master"
                return

            self.execute(" ".join([
                'kubeadm',
                'init',
                '--token=%s' % token,
                '--token-ttl=0',
                '--apiserver-advertise-address=%s' % ip,
                '--pod-network-cidr=10.244.0.0/16',
                '--kubernetes-version=v1.10.2'
            ]))

            with open("/etc/kubernetes/admin.conf", "r") as config_file:
                config = yaml.safe_load(config_file)

            config["clusters"][0]["cluster"]["server"] = 'https://%s:6443' % ip
            config["clusters"][0]["name"] = self.node
            config["users"][0]["name"] = self.node
            config["contexts"][0]["name"] = self.node
            config["contexts"][0]["context"]["cluster"] = self.node
            config["contexts"][0]["context"]["user"] = self.node
            config["current-context"] = self.node

        elif self.config["kubernetes"]["role"] == "worker":

            self.host("%s-%s-klot-io" % (self.config["kubernetes"]["name"], self.config["kubernetes"]["cluster"]))

            if os.path.exists("/etc/kubernetes/bootstrap-kubelet.conf"):
                print "already initialized worker"
                return

            self.execute(" ".join([
                'kubeadm',
                'join',
                '%s:6443' % socket.gethostbyname('%s-klot-io.local' % self.config["kubernetes"]["cluster"]),
                '--token=%s' % token,
                '--discovery-token-unsafe-skip-ca-verification'
            ]))

            config = requests.get(
                'http://%s-klot-io.local/api/kubectl' % self.config["kubernetes"]["cluster"],
                headers={"x-klot-io-password": self.config["account"]['password']},
            ).json()["kubectl"]

        self.execute("mkdir -p /home/pi/.kube")
        self.execute("rm -f /home/pi/.kube/config")
        
        with open("/home/pi/.kube/config", "w") as config_file:
            yaml.safe_dump(config, config_file, default_flow_style=False)

        self.execute("chown pi:pi /home/pi/.kube/config")

        if self.config["kubernetes"]["role"] == "master":
            self.execute("sudo -u pi -- kubectl apply -f /opt/klot-io/kubernetes/kube-flannel.yml")
            self.execute("sudo -u pi -- kubectl apply -f /opt/klot-io/kubernetes/klot-io-app-crd.yaml")

    def resources(self, obj):

        if "resources" in obj:
            return

        obj["resources"] = []

        for manifest in obj["spec"]["manifests"]:

            source = copy.deepcopy(obj["spec"]["source"])
            source.update(manifest)

            print "parsing %s" % source

            if "url" in source:

                url = source["url"]

            elif "site" in source and source["site"] == "github.com":

                if "repo" not in source:
                    raise AppException("missing source.repo for %s" % source["site"])

                repo = source["repo"]
                version = source["version"] if "version" in source else "master"
                path = source["path"] if "path" in source else "klot-io-app.yaml"

                url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, version, path)

            else:

                raise AppException("cannot parse %s" % source)

            print "fetching %s" % url

            response = requests.get(url)

            if response.status_code != 200:
                raise AppException("%s error from %s: %s" % (response.status_code, url, response.text))

            obj["resources"].extend(list(yaml.safe_load_all(response.text)))

        obj["resources"].sort(key= lambda resource: RESOURCES.index(resource["kind"]) if resource["kind"] in RESOURCES else len(RESOURCES))

    def display(self, obj):

        display = [obj["kind"]]

        if "namespace" in obj["metadata"] and obj["metadata"]["namespace"]:
            display.append(obj["metadata"]["namespace"])

        display.append(obj["metadata"]["name"])

        return "/".join(display)

    def app(self, source, action):

        print "searching for %s" % source

        for app in [app.obj for app in pykube.App.objects(self.kube).filter()]:

            match = True
            for field in source:
                if field not in app["spec"]["source"] or source[field] != app["spec"]["source"][field]:
                    match = False

            if match:
                print "found %s %s" % (app["metadata"]["name"], app["spec"]["source"])
                return app

        print "creating %s" % source

        if "url" in source:

            url = source["url"]

        elif "site" in source and source["site"] == "github.com":

            if "repo" not in source:
                raise Exception("missing source.repo for %s" % source["site"])

            repo = source["repo"]
            version = source["version"] if "version" in source else "master"
            path = source["path"] if "path" in source else "klot-io-app.yaml"

            url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, version, path)

        else:

            raise Exception("cannot preview %s" % source)

        print "requesting %s" % url

        response = requests.get(url)

        if response.status_code != 200:
            raise Exception("error from source %s url: %s - %s: %s" % (source, url, response.status_code, response.text)) 

        obj = yaml.safe_load(response.text)

        if (
            not isinstance(obj, dict) or obj["apiVersion"] != "klot.io/v1" or obj["kind"] != "App" or 
            "metadata" not in obj or "spec" not in obj or len(obj.keys()) != 4
        ):
            raise Exception("source %s has malformed App %s" % (source, obj))

        obj["action"] = action
        obj["status"] = "Discovered"

        pykube.App(self.kube, obj).create()

        return obj

    def source(self, obj):

        if "subscribe" in obj["spec"]:

            for subscribe in obj["spec"]["subscribe"]:

                print "sourcing %s" % subscribe

                app = self.app(subscribe["source"], obj["action"])

                print "sourced %s %s" % (app["metadata"]["name"], app["spec"]["source"])

    def subscribe(self, obj):

        subscribed = True

        if "subscribe" in obj["spec"]:

            obj["subscriptions"] = []

            for subscribe in obj["spec"]["subscribe"]:

                print "subscribing %s" % subscribe

                app = self.app(subscribe["source"], obj["action"])

                if app["status"] != "Installed":

                    subscribed = False

                    if obj["action"] == "Install" and app["status"] != "Error":
                        app["action"] = "Install"
                        pykube.App(self.kube, app).replace()

                subscription = {
                    "name": subscribe["name"],
                    "app": app["metadata"]["name"],
                    "project": subscribe["project"]
                }

                print "subscribed %s" % subscription

                obj["subscriptions"].append(subscription)

        return subscribed

    def project(self, obj):

        if "subscriptions" not in obj:
            return

        for subscription in obj["subscriptions"]:

            if "project" not in subscription:
                continue

            project = subscription["project"]

            print "projecting %s" % project

            app = pykube.App.objects(self.kube).filter().get(name=subscription["app"]).obj

            for publication in app["publications"]:

                if publication["name"] == project["publication"]:

                    encoding = project.get("encoding", "yaml")
                    key = project.get("key", "%s.%s" % (subscription["name"], encoding))

                    config = pykube.ConfigMap.objects(self.kube).filter(namespace=project["namespace"]).get(name=project["name"]).obj

                    if "data" not in config:
                        config["data"] = {}

                    if encoding == "json":
                        config["data"][key] = json.dumps(publication, indent=2)
                    else:
                        config["data"][key] = yaml.safe_dump(publication, default_flow_style=False)

                    pykube.ConfigMap(self.kube, config).replace()

                    break

    def publish(self, obj):

        if "publish" not in obj["spec"]:
            return

        obj["publications"] = []

        for spec in obj["spec"]["publish"]:

            print "publishing %s" % spec

            publication = {}

            for field in spec:

                if field == "service":

                    service = pykube.Service.objects(self.kube).filter(
                        namespace=spec["service"]["namespace"]
                    ).get(name=spec["service"]["name"]).obj

                    publication["host"] = "%s.%s" % (service["metadata"]["name"], service["metadata"]["namespace"])

                    if "port" in spec["service"]:
                        for port in service["spec"]["ports"]:
                            if "name" in port and port["name"] == spec["service"]["port"]:
                                publication["port"] = port["port"]

                    if service["spec"]["type"] == "LoadBalancer":
                        publication["ingress"] = True

                else:

                    publication[field] = spec[field]

            print "published %s" % publication

            obj["publications"].append(publication)

    def url(self, obj):

        if "url" not in obj["spec"] or "publications" not in obj:
            return

        print "creating url %s " % obj["spec"]["url"]

        for publication in obj["publications"]:

            if obj["spec"]["url"]["publication"] == publication["name"]:

                obj["url"] = "%s://%s.%s.local" % (publication["protocol"], publication["host"], self.node)

                if (
                    publication["protocol"] == "http" and publication["port"] != 80 or 
                    publication["protocol"] == "https" and publication["port"] != 443
                ):
                    obj["url"] = "%s:%s" % (obj["url"], obj["port"])

                if "path" in obj["spec"]["url"]:
                    obj["url"] = "%s/%s" % (obj["url"], obj["path"])

                print "created url %s " % obj["url"]

    def apps(self):

        for obj in [app.obj for app in pykube.App.objects(self.kube).filter()]:

            try:

                if "status" not in obj:
                    obj["status"] = "Discovered"

                if "action" not in obj:
                    obj["action"] = "Download"

                if obj["status"] == "Discovered" and "resources" not in obj:
                    self.resources(obj)
                    self.source(obj)
                    obj["status"] = "Downloaded"

                if obj["action"] == "Install" and obj["status"] not in ["Installed", "Error"] and self.subscribe(obj):

                    print "installing %s" % self.display(obj)
                    for resource in obj["resources"]:
                        print "applying %s" % self.display(resource)
                        Resource = getattr(pykube, resource["kind"])
                        try:
                            Resource(self.kube, resource).replace()
                        except pykube.PyKubeError:
                            Resource(self.kube, resource).delete()
                            Resource(self.kube, resource).create()

                    self.project(obj)
                    self.publish(obj)
                    self.url(obj)

                    obj["status"] = "Installed"

                elif obj["status"] == "Installed" and obj["action"] == "Uninstall":

                    print "uninstalling %s" % self.display(obj)
                    for resource in reversed(obj["resources"]):
                        print "deleting %s" % self.display(resource)
                        getattr(pykube, resource["kind"])(self.kube, resource).delete()
                    if "subscriptions" in obj:
                        del obj["subscriptions"]
                    if "publications" in obj:
                        del obj["publications"]
                    if "url" in obj:
                        del obj["url"]

                    obj["action"] = "Download"
                    obj["status"] = "Downloaded"

            except Exception as exception:

                obj["status"] = "Error"
                obj["error"] = traceback.format_exc().splitlines()
                traceback.print_exc()

            pykube.App(self.kube, obj).replace()

    def nginx(self, expected):

        actual = {}

        for nginx_path in glob.glob("/etc/nginx/conf.d/*.conf"):

            host = nginx_path.split("/")[-1].split(".conf")[0]
            external = None
            actual[host] = {"servers": []}

            with open(nginx_path, "r") as nginx_file:
                for nginx_line in nginx_file:
                    if "listen" in nginx_line:
                        external = int(nginx_line.split()[-1][:-1])
                    if "proxy_pass" in nginx_line:
                        actual[host]["servers"].append({
                            "protocol": nginx_line.split(":")[0].split(" ")[-1],
                            "external": external,
                            "internal": int(nginx_line.split(":")[-1].split("/")[0])
                        })
                        actual[host]["ip"] = nginx_line.split("/")[2].split(":")[0]

        if expected != actual:
        
            self.differs(expected, actual)
            self.execute("rm -f /etc/nginx/conf.d/*.conf")

            for host in expected:
                with open("/etc/nginx/conf.d/%s.conf" % host, "w") as nginx_file:
                    for server in expected[host]["servers"]:
                        nginx_file.write(SERVER % (server["external"], host, server["protocol"], expected[host]["ip"], server["internal"]))

            self.execute("systemctl reload nginx")

    def services(self):

        nginx = {}
        cnames = set()

        for service in [service.obj for service in pykube.Service.objects(self.kube).filter(namespace=pykube.all)]:

            if (
                "type" not in service["spec"] or service["spec"]["type"] != "LoadBalancer" or 
                "ports" not in service["spec"] or "selector" not in service["spec"] or 
                "namespace" not in service["metadata"]
            ):
                continue

            servers = []

            for port in service["spec"]["ports"]:

                if "name" not in port:
                    continue

                if port["name"].lower().startswith("https"):
                    servers.append({
                        "protocol": "https",
                        "external": port["port"],
                        "internal": port["targetPort"]
                    })
                elif port["name"].lower().startswith("http"):
                    servers.append({
                        "protocol": "http",
                        "external": port["port"],
                        "internal": port["targetPort"]
                    })

            if not servers:
                continue

            node_ips = {}

            for pod in [pod.obj for pod in pykube.Pod.objects(self.kube).filter(
                namespace=service["metadata"]["namespace"], 
                selector=service["spec"]["selector"]
            )]:
                if "nodeName" in pod["spec"] and "podIP" in pod["status"]:
                    node_ips[pod["spec"]["nodeName"]] = pod["status"]["podIP"]

            if not node_ips or sorted(node_ips.keys())[0] != self.node:
                continue

            ip = node_ips[self.node]

            host = ("%s.%s.%s-klot-io.local" % (
                service["metadata"]["name"],
                service["metadata"]["namespace"],
                self.config["kubernetes"]["cluster"]
            ))

            cnames.add(host)
            nginx[host] = {
                "ip": ip,
                "servers": servers
            }

        if cnames != self.cnames:
            self.differs(cnames, self.cnames)
            self.cnames = cnames
            self.avahi()

        self.nginx(nginx)

    def clean(self):

        past = time.time() - 60

        for tmp_file in list(glob.glob("/tmp/tmp??????")):
            if past > os.path.getmtime(tmp_file):
                os.remove(tmp_file)

    def process(self):

        if os.path.exists("/boot/klot-io/reset"):
            self.reset()

        if os.path.exists("/boot/klot-io/lib/config.py"):
            self.restart()

        self.reload()
        self.load()

        if "account" in self.modified:
            self.account()

        if "network" in self.modified:
            self.network()

        if "kubernetes" in self.modified:
            self.kubernetes()

        if not self.kube and os.path.exists("/home/pi/.kube/config"):
            self.kube = pykube.HTTPClient(pykube.KubeConfig.from_file("/home/pi/.kube/config"))

        if self.kube:

            if self.config["kubernetes"]["role"] == "master":
                self.apps()

            self.services()
            self.clean()

    def run(self):

        while True:

            try:

                self.process()

            except Exception as exception:

                traceback.print_exc()

            time.sleep(5)
