#!/usr/bin/env bash

# # per-user installation
# mkdir -p ~/.docker/cli-plugins
# curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m) -o ~/.docker/cli-plugins/docker-compose
# chmod +x ~/.docker/cli-plugins/docker-compose

# # newer docker plugin
# curl -SL https://github.com/docker/buildx/releases/download/v0.32.1/buildx-v0.32.1.linux-amd64 -o ~/.docker/cli-plugins/docker-buildx
# chmod +x ~/.docker/cli-plugins/docker-buildx

# sudo groupadd -r docker
# sudo usermod -aG docker $USER  # restart to have it take effect, so you won't need to constantly run the command below
# newgrp docker
# 

# install docker + docker-compose
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

