#!/bin/sh
ansible-galaxy role install -r requirements.yml -p vendor/roles
ansible-galaxy collection install -r requirements.yml -p vendor/collections
