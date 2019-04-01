#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# imgbase
#
# Copyright (C) 2016  Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Author(s): Ryan Barry <rbarry@redhat.com>
#

import os
import uuid
import logging
import glob
import socket
import subprocess

from .. import utils
from ..utils import File, mounted, SystemRelease, SELinuxDomain


log = logging.getLogger(__package__)


class RpmPersistenceError(Exception):
    pass


def pre_init(app):
    app.imgbase.hooks.create("rpms-persisted",
                             ("previous-lv_fullname", "new-lv_fullname"))


def init(app):
    app.imgbase.hooks.connect("os-upgraded", on_os_upgraded)


def on_os_upgraded(imgbase, previous_lv_name, new_lv_name):
    log.debug("Got: %s and %s" % (new_lv_name, previous_lv_name))

    # FIXME this can be improved by providing a better methods in .naming
    new_layer = imgbase.image_from_lvm_name(new_lv_name)
    new_lv = imgbase.lv_from_layer(new_layer)
    previous_layer_lv = \
        imgbase._lvm_from_layer(imgbase.naming.layer_before(new_layer))
    try:
        reinstall_rpms(imgbase, new_lv, previous_layer_lv)
    except:
        log.exception("Failed to reinstall persisted RPMs")
        raise RpmPersistenceError()


def reinstall_rpms(imgbase, new_lv, previous_lv):
    # FIXME: this should get moved to a generalized plugin. We need to check
    # it in multiple places
    if "inst.ks" not in File('/proc/cmdline').contents:
        with mounted(new_lv.path) as new_fs:
            new_etc = new_fs.path("/etc")

            new_rel = SystemRelease(new_etc + "/system-release-cpe")

            if not new_rel.is_supported_product():
                log.error("Unsupported product: %s" % new_rel)
                raise RpmPersistenceError()

            with utils.bindmounted("/var", target=new_fs.target + "/var"):
                install_rpms(new_fs)

        log.info("Not reinstalling RPMs during system installation")

    imgbase.hooks.emit("rpms-persisted",
                       previous_lv.lv_name,
                       new_lv.lvm_name)


def install_rpms(new_fs):
    # Just use `rpm -Uvh`, so we can avoid setting up a local yum repo
    # all dependencies will be saved anyway, and a local yum repo doesn't
    # add a significant benefit unless we also track a list of packages
    # somewhere
    #
    # e.g. `yum install zsh` keeps track of `zsh` in some file, then we
    # set up a local repo and `yum -y localinstall foo bar quux`, with
    # the deps autoresolving
    def install(args):
        cmd = ["systemd-nspawn",
               "--uuid", str(uuid.uuid4()).replace("-", ""),
               "--machine", socket.getfqdn(),
               "-D", new_fs.path("/")] + args
        log.debug("Running %s" % cmd)
        try:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            log.info("Failed to reinstall persisted RPMs!")
            log.info("Result: " + e.output)

    rpms = glob.glob("/var/imgbased/persisted-rpms/*.rpm")
    if rpms:
        machine_id = new_fs.path("/etc") + "/machine-id"
        backup = machine_id + ".bak"
        os.rename(machine_id, backup)
        with SELinuxDomain("systemd_machined_t"):
            install(['rpm', '-Uvh'] + rpms)
        os.rename(backup, machine_id)
