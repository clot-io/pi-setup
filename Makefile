ACCOUNT=klotio
IMAGE=pi-setup
VERSION?=0.1
VOLUMES=-v ${PWD}/requirements.txt:/opt/klot-io/requirements.txt \
        -v ${PWD}/etc/:/opt/klot-io/etc/\
        -v ${PWD}/lib/:/opt/klot-io/lib/ \
        -v ${PWD}/www/:/opt/klot-io/www/ \
        -v ${PWD}/bin/:/opt/klot-io/bin/ \
        -v ${PWD}/config/:/opt/klot-io/config/ \
        -v ${PWD}/service/:/opt/klot-io/service/ \
        -v ${PWD}/images/:/opt/klot-io/images/ \
		-v ${PWD}/secret/:/opt/klot-io/secret/
PORT=8083
HOST?=klot-io.local


.PHONY: build shell boot api daemon gui export shrink config clean kubectl

build:
	docker build . -f Dockerfile.setup -t $(ACCOUNT)/$(IMAGE)-setup:$(VERSION)

shell:
	docker run --privileged=true -it --network=host $(VARIABLES) $(VOLUMES) $(ACCOUNT)/$(IMAGE)-setup:$(VERSION) sh

boot:
	docker run --privileged=true -it --rm -v /Volumes/boot/:/opt/klot-io/boot/ $(VOLUMES) $(ACCOUNT)/$(IMAGE)-setup:$(VERSION) sh -c "bin/boot.py $(VERSION)"

api:
	scp lib/manage.py pi@$(HOST):/opt/klot-io/lib/
	ssh pi@$(HOST) "sudo systemctl restart klot-io-api"

daemon:
	scp lib/config.py pi@$(HOST):/opt/klot-io/lib/
	ssh pi@$(HOST) "sudo systemctl restart klot-io-daemon"

gui:
	scp -r www pi@$(HOST):/opt/klot-io/
	ssh pi@$(HOST) "sudo systemctl reload nginx"

export:
	bin/export.sh $(VERSION)

shrink:
	docker run --privileged=true -it --rm $(VOLUMES) $(ACCOUNT)/$(IMAGE):$(VERSION) sh -c "pishrink.sh images/pi-$(VERSION).img"

config:
	cp config/*.yaml /Volumes/boot/klot-io/config/
	docker-compose -f docker-compose.yml up

clean:
	docker-compose -f docker-compose.yml down

kubectl:
ifeq (,$(wildcard /usr/local/bin/kubectl))
	curl -LO https://storage.googleapis.com/kubernetes-release/release/$(KUBECTL_VERSION)/bin/darwin/amd64/kubectl
	chmod +x ./kubectl
	sudo mv ./kubectl /usr/local/bin/kubectl
endif
	mkdir -p secret
	[ -f ~/.kube/config ] && cp ~/.kube/config secret/kubectl || [ ! -f ~/.kube/config ]
	docker run -it $(VARIABLES) $(VOLUMES) $(ACCOUNT)/$(IMAGE)-setup:$(VERSION) bin/kubectl.py
	cp secret/kubectl ~/.kube/config