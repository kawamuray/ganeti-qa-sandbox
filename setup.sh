#!/bin/sh
set -e

scp *.json root@192.168.189.106:
ssh root@192.168.189.106 brctl addbr br0
