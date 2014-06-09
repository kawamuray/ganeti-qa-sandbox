#!/usr/bin/env python
import errno
import glob
import os
import shutil
import stat
import subprocess
import sys
import time
import json
import yaml

# TODO ssh key verification fail

with open('./config.yml', 'r') as f:
    Config = yaml.load(f)

class LXC(object):
    ROOTFS_TEMPLATE = './rootfs'
    MAKEDIRS = set([
        '/proc',
        '/dev',
        '/dev/pts',
        '/dev/shm',
        '/dev/mapper',
        '/sys',
        # '/sys',
        # '/tmp',
        '/var/log',
        '/var/empty', # required by sshd
        '/usr/local/var/log',
        '/usr/local/var/lib',
        '/usr/local/var/run',
        '/usr/local/var/lock',
        # '/run',
    ])
    BIND_MOUNT_DIRS = set([
        '/bin',
        '/lib',
        '/lib32',
        '/lib64',
        '/sbin',
        '/usr/bin',
        '/usr/lib',
        '/usr/lib32',
        '/usr/lib64',
        '/usr/libexec',
        '/usr/sbin',
        '/usr/share',
        '/usr/local/bin',
        # '/usr/local/etc',
        '/usr/local/lib',
        '/usr/local/lib32',
        '/usr/local/lib64',
        '/usr/local/libexec',
        '/usr/local/sbin',
        '/usr/local/share',
        '/dev/mapper',
    ])
    REPLICATE_DEVS = set([
        '/dev/console',
        '/dev/full',
        '/dev/null',
        '/dev/random',
        '/dev/tty',
        '/dev/urandom',
        '/dev/zero',
    ] + [
        '/dev/loop%d' % x for x in range(0, 8)
    ] + [
        '/dev/tty%d' % x for x in range(1, 7)
    ])

    def __init__(self, name, address):
        self.name = name
        self.address = address
        self.rootfs = os.path.join(Config['containers']['root'], self.name)
        self.config_file = self.rootfs + '.conf'
        self.console_socket = self.rootfs + '.console'
        self.process = None

    def inside_path(self, path):
        if os.path.isabs(path):
            path = path[1:] # discard '/'
        return os.path.join(self.rootfs, path)

    def mkdir_p(self, path):
        try:
            os.makedirs(path)
        except OSError as err:
            if err.errno != errno.EEXIST:
                raise err

    def replicate_node(self, path):
        inside_path = self.inside_path(path)
        if not os.path.exists(inside_path):
            st = os.stat(path)
            os.mknod(inside_path, st.st_mode, st.st_rdev)

    def mount_read_only(self, src, dest):
        self.mkdir_p(dest)
        # Do not use --rbind instead of --bind. sub-mount directories will not be
        # read-only mode and you will see the hell :P
        subprocess.call(['mount', '--bind', src, dest])
        # This is really required to make mount to be read-only mode
        subprocess.call(['mount', '-o', 'remount,ro', dest])

    def write_config(self, config):
        with open(self.config_file, 'w') as f:
            for k, v in config:
                f.write("lxc.%s = %s\n" % (k, v))

    def write_file(self, path, content):
        inside_path = self.inside_path(path)
        self.mkdir_p(os.path.dirname(inside_path))
        with open(inside_path, 'w') as f:
            f.write(content)

    def prepare(self):
        self.write_config([
            ('utsname', self.name),
            ('rootfs', self.rootfs),
            ('tty', 6),
            ('pts', 128),
            ('console', self.console_socket),
            ('mount.entry', 'proc proc proc nosuid,nodev,noexec  0 0'),
            ('mount.entry', 'sysfs sys sysfs nosuid,nodev,noexec,ro 0 0'),
            ('mount.entry', 'devpts dev/pts devpts nosuid,noexec,mode=0620,ptmxmode=000,newinstance 0 0'),
            ('mount.entry', 'tmpfs dev/shm tmpfs nosuid,nodev,mode=1777 0 0'),
            # ('mount.entry', 'tmpfs run tmpfs nosuid,nodev,noexec,mode=0755,size=128m 0 0'),
            # ('mount.entry', 'tmpfs tmp tmpfs nosuid,nodev,noexec,mode=1777,size=1g 0 0'),
            ('cgroup.devices.allow', 'a'),
        ] + [x for y in [
            [('network.type', 'veth'),
             ('network.link', Config['containers']['network']['bridge']),
             ('network.ipv4', x),
             ('network.flags', 'up')]
            for x in self.address
        ] for x in y]
        )
        for d in glob.glob(os.path.join(self.ROOTFS_TEMPLATE, '*')):
            print d
            shutil.copytree(d, self.inside_path(os.path.basename(d)), symlinks=True)
        for d in self.MAKEDIRS:
            self.mkdir_p(self.inside_path(d))
        for d in self.BIND_MOUNT_DIRS:
            if os.path.isdir(d):
                self.mount_read_only(d, self.inside_path(d))
        # Create device files
        for dev in self.REPLICATE_DEVS:
            self.replicate_node(dev)
        # stdio devices
        for fd, dev in ((0, 'stdin'), (1, 'stdout'), (2, 'stderr')):
            os.symlink('/proc/self/fd/%d' % fd,
                       self.inside_path('/dev/%s' % dev))
        # /tmp
        tmpdir_path = self.inside_path('/tmp')
        self.mkdir_p(tmpdir_path)
        os.chmod(tmpdir_path, 01777)

    def start(self):
        cmd = [
            'lxc-start',
            '-o', '/dev/stdout',
            '-l', 'DEBUG',
            '-n', self.name,
            '-f', self.config_file,
        ]
        self.process = subprocess.Popen(cmd)

    def stop(self):
        if self.process:
            print >>sys.stderr, "killing container %s ..." % self.name
            subprocess.call(['lxc-stop', '-n', self.name])
            self.process.wait()

    def destroy(self):
        for d in self.BIND_MOUNT_DIRS:
            subprocess.call(['umount', '-l', self.inside_path(d)])
        subprocess.call(['umount', '-l', self.inside_path('/root/ganeti')])

if __name__ == '__main__':
    containers = []
    try:
        namecount = {}
        for ndconf in Config['nodes']:
            seqno = namecount.get(ndconf['type'], 0)
            namecount[ndconf['type']] = seqno + 1

            name = '.'.join([ndconf['type'] + str(seqno), 'qa-sandbox', 'ganeti'])
            print name
            container = LXC(name, (ndconf['address']['primary'], ndconf['address']['secondary']))
            containers.append(container)
            container.prepare()
            
        # /etc/hosts for all hosts
        hosts = ''.join("\t".join(x) + "\n" for x in
                        [('127.0.0.1', 'localhost'),
                         # TODO be configurable
                         # ('192.168.189.104', 'cluster.qa-sandbox.ganeti')] +
                         ('192.168.189.104', 'qa-lxc')] +
                        [(c.address[0], c.name) for c in containers])
        known_hosts = ''.join(["%s,%s ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILSWsBTHv9MbcIqQVOJTAqqpTmeB26UjFm7aTbjPHLuf\n"
                               % (c.name, c.address[0]) for c in containers])
        for container in containers:
            container.write_file('/etc/hosts', hosts)
            for key_file in ('id_rsa', 'id_rsa.pub'):
                inside_key_file = container.inside_path('/root/.ssh/' + key_file)
                shutil.copyfile(key_file, inside_key_file)
                os.chmod(inside_key_file, 0400)
            with open('id_rsa.pub', 'r') as f:
                pub_key = f.read()
            with open(container.inside_path('/root/.ssh/authorized_keys'), 'a') as f:
                f.write(pub_key)
            # with open(container.inside_path('/root/.ssh/config'), 'a') as f:
            #     f.write("Host *\nStrictHostKeyChecking no\n")
            with open(container.inside_path('/root/.ssh/known_hosts'), 'a') as f:
                f.write(known_hosts)
            with open(container.inside_path('/root/.bashrc'), 'a') as f:
                f.write("export PATH=/usr/local/sbin:$PATH\n")
            container.start()
        print containers
        while True:
            time.sleep(1 << 32)
    finally:
        for container in containers:
            container.stop()
            container.destroy()
        # TODO unmount all, remove garbages
