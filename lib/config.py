import os
import time
import glob
import yaml
import socket
import hashlib
import netifaces
import traceback

WPA = """ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=%s

network={
\tscan_ssid=1
\tssid="%s"
\t%s
}
"""

class Daemon(object):

    def __init__(self):

       self.config = {}
       self.mtimes = {}
       self.modified = []

    def execute(self, command):

        print command
        os.system(command)

    def reset(self):

        print "reseting"

        self.execute("rm /boot/klot-io/reset")

        with open("/opt/klot-io/config/account.yaml", "w") as yaml_file:
            yaml.safe_dump({"password": "kloudofthings"}, yaml_file, default_flow_style=False)

        with open("/opt/klot-io/config/network.yaml", "w") as yaml_file:
            yaml.safe_dump({"interface": "eth0"}, yaml_file, default_flow_style=False)

        with open("/opt/klot-io/config/kubernetes.yaml", "w") as yaml_file:
            yaml.safe_dump({"role": "reset"}, yaml_file, default_flow_style=False)

    def restart(self):
        
        print "restarting"

        self.execute("cp /boot/klot-io/bin/daemon.py /opt/klot-io/bin/daemon.py")
        self.execute("chown 1000:1000 /opt/klot-io/bin/daemon.py")
        self.execute("chmod a+x /opt/klot-io/bin/daemon.py")

        self.execute("rm /boot/klot-io/bin/daemon.py")

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
                    self.config[config] = yaml.load(yaml_file)

                self.mtimes[config] = mtime
                self.modified.append(config)

    def differs(self, expected, actual):

        print "actual:   %s" % actual
        print "expected: %s" % expected

        return expected != actual

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
            self.execute("service avahi-daemon restart")

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

        avahi = False

        with open("/etc/hostname", "r") as hostname_file:
            actual = hostname_file.read()

        if self.differs(expected, actual):

            with open("/etc/hostname", "w") as hostname_file:
                hostname = hostname_file.write(expected)

            self.execute("hostnamectl set-hostname %s" % expected)

            avahi = True

        with open("/etc/hosts", "r") as hosts_file:
            actual = hosts_file.readlines()[-1].split("\t")[-1].strip()

        if self.differs(expected, actual):
            self.execute("sed -i 's/127.0.1.1\t.*/127.0.1.1\t%s/' /etc/hosts" % expected)
            avahi = True

        if avahi:
            self.execute("service avahi-daemon restart")

    def kubernetes(self):

        if self.config["kubernetes"]["role"] == "reset":
            self.host("klot-io")
            self.execute("kubeadm reset")
            self.execute("rm /opt/klot-io/config/kubernetes.yaml")
            self.execute("rm /home/pi/.kube/config")
            self.execute("reboot")
            return

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

            host = '%s-klot-io' % self.config["kubernetes"]["cluster"]
            self.host(host)

            self.execute(" ".join([
                'kubeadm',
                'init',
                '--token=%s' % token,
                '--token-ttl=0',
                '--apiserver-advertise-address=%s' % ip,
                '--pod-network-cidr=10.244.0.0/16',
                '--kubernetes-version=v1.10.2'
            ]))

            self.execute("mkdir -p /home/pi/.kube")
            self.execute("rm -f /home/pi/.kube/config")
            
            with open("/etc/kubernetes/admin.conf", "r") as config_file:
                config = yaml.load(config_file)

            config["clusters"][0]["cluster"]["server"] = 'https://%s:6443' % ip
            config["clusters"][0]["name"] = host
            config["users"][0]["name"] = host
            config["contexts"][0]["name"] = host
            config["contexts"][0]["context"]["cluster"] = host
            config["contexts"][0]["context"]["user"] = host
            config["current-context"] = host

            with open("/home/pi/.kube/config", "w") as config_file:
                yaml.safe_dump(config, config_file, default_flow_style=False)

            self.execute("chown pi:pi /home/pi/.kube/config")
            self.execute("sudo -u pi -- kubectl apply -f /opt/klot-io/config/kube-flannel.yml")

        elif self.config["kubernetes"]["role"] == "worker":

            self.host("%s-%s-klot-io" % (self.config["kubernetes"]["name"], self.config["kubernetes"]["cluster"]))

            self.execute(" ".join([
                'kubeadm',
                'join',
                '%s:6443' % socket.gethostbyname('%s-klot-io.local' % self.config["kubernetes"]["cluster"]),
                '--token=%s' % token,
                '--discovery-token-unsafe-skip-ca-verification'
            ]))

    def process(self):

        if os.path.exists("/boot/klot-io/reset"):
            self.reset()

        if os.path.exists("/boot/klot-io/bin/daemon.py"):
            self.restart()

        self.reload()
        self.load()

        if "account" in self.modified:
            self.account()

        if "network" in self.modified:
            self.network()

        if "kubernetes" in self.modified:
            self.kubernetes()

    def run(self):

        while True:

            try:

                self.process()

            except Exception as exception:

                traceback.print_exc()

            time.sleep(5)
