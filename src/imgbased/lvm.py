#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# imgbase
#
# Copyright (C) 2014  Red Hat, Inc.
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
# Author(s): Fabian Deutsch <fabiand@redhat.com>
#
import logging
import os
import re
import shlex
from operator import itemgetter

from .utils import ExternalBinary, LvmCLI, find_mount_source

log = logging.getLogger(__package__)


class MissingLvmThinPool(Exception):
    pass


class ThinPoolMetadataError(Exception):
    pass


class LVM(object):
    _lvs = LvmCLI.lvs
    _vgs = LvmCLI.vgs
    _lvcreate = LvmCLI.lvcreate
    _lvchange = LvmCLI.lvchange
    _lvremove = LvmCLI.lvremove
    _lvrename = LvmCLI.lvrename
    _lvextend = LvmCLI.lvextend
    _vgcreate = LvmCLI.vgcreate
    _vgchange = LvmCLI.vgchange
    _lvmconfig = LvmCLI.lvmconfig
    _volume_registry = []

    @staticmethod
    def _list_lv_full_names(filtr=""):
        cmd = ["--noheadings", "--ignoreskippedcluster", "-o", "lv_full_name"]
        if filtr:
            cmd = ["--noheadings",
                   "--ignoreskippedcluster",
                   "-o",
                   "lv_full_name",
                   "--select",
                   filtr]
        raw = LVM._lvs(cmd)
        names = sorted(n.strip() for n in raw.splitlines())
        log.debug("All LV names: %s" % names)
        return names

    @classmethod
    def list_lvs(cls, filtr=""):
        lvs = [cls.LV.from_lvm_name(n) for n in cls._list_lv_full_names(filtr)]
        log.debug("All LVS: %s" % lvs)
        return lvs

    @staticmethod
    def is_name_valid(name):
        """Taken from blivet
        """
        # No . or ..
        if name == '.' or name == '..':
            return False

        # Check that all characters are in the allowed set and that the name
        # does not start with a -
        if not re.match('^[a-zA-Z0-9+_.][a-zA-Z0-9+_.-]*$', name):
            return False

        # According to the LVM developers, vgname + lvname is limited to
        # 126 characters
        # minus the number of hyphens, and possibly minus up to another
        # 8 characters
        # in some unspecified set of situations. Instead of figuring all
        # of that out,
        # no one gets a vg or lv name longer than, let's say, 55.
        if len(name) > 55:
            return False

        return True

    @staticmethod
    def register_volume(vol):
        assert isinstance(vol, LVM.LV)
        LVM._volume_registry.append(vol)
        return vol

    @staticmethod
    def reset_registered_volumes():
        if os.getenv("IMGBASED_KEEP_VOLUMES"):
            return
        run = ExternalBinary()
        run.sync([])
        mtab = dict([itemgetter(9, 4)(m.split())
                     for m in open("/proc/self/mountinfo")])
        for lv in LVM._volume_registry:
            target = mtab.get(lv.dm_path)
            if target:
                run.umount([target])
            try:
                lv.remove(force=True)
            except Exception:
                log.debug("Failed removing LV [%s], skipping", lv.dm_path)

    @staticmethod
    def stop_monitoring():
        LVM._vgchange(["--monitor", "n"])
        ExternalBinary().pkill(["dmeventd"])

    class VG(object):
        vg_name = None

        def __init__(self, vg_name):
            self.vg_name = vg_name

        def __repr__(self):
            return "<VG '%s' />" % self.vg_name

        @staticmethod
        def from_vg_name(vg_name):
            vg = LVM.VG(vg_name)
            return vg

        @staticmethod
        def find_by_tag(tag):
            vgs = LVM._vgs(["--noheadings", "--ignoreskippedcluster",
                            "--select", "vg_tags = %s" % tag, "-o", "vg_name"])
            return [LVM.VG(vg_name.strip()) for vg_name in vgs.splitlines()]

        @staticmethod
        def from_tag(tag):
            vgs = LVM.VG.find_by_tag(tag)
            assert len(vgs) == 1
            return vgs[0]

        @staticmethod
        def create(vg_name, pv_paths):
            assert LVM.is_name_valid(vg_name)
            LVM._vgcreate([vg_name] + pv_paths)
            return LVM.VG(vg_name)

        def deltag(self, tag):
            LVM._vgchange(["--deltag", tag, self.vg_name])

        def addtag(self, tag):
            LVM._vgchange(["--addtag", tag, self.vg_name])

        def tags(self):
            return LVM._vgs(["--noheadings", "--ignoreskippedcluster",
                             "-ovg_tags", self.vg_name]).split(",")

    class LV(object):
        vg_name = None
        lv_name = None

        @property
        def lvm_name(self):
            """With lvm_name we referre to the combination of VG+LV: VG/LV
            """
            return "%s/%s" % (self.vg_name, self.lv_name)

        @property
        def path(self):
            return LVM._lvs(["--noheadings", "--ignoreskippedcluster",
                             "-olv_path", self.lvm_name])

        @property
        def dm_path(self):
            return LVM._lvs(["--noheadings", "--ignoreskippedcluster",
                             "-olv_dm_path", self.lvm_name])

        @property
        def size_bytes(self):
            return LVM._lvs(["--noheadings", "--ignoreskippedcluster",
                             "-osize", "--units", "B", self.lvm_name])

        @classmethod
        def from_lv_name(cls, vg_name, lv_name):
            lv = cls()
            lv.vg_name = vg_name
            lv.lv_name = lv_name
            return lv

        def __repr__(self):
            return "<LV '%s' />" % self.lvm_name

        @classmethod
        def try_find(cls, mixed):
            log.debug("Trying to find LV for: %s" % mixed)
            assert mixed
            if mixed.startswith("/dev"):
                return cls.from_path(mixed)
            elif os.path.ismount(mixed):
                return cls.from_path(find_mount_source(mixed))
            elif "/" in mixed:
                return cls.from_lvm_name(mixed)
            elif "@" in mixed:
                return cls.from_tag(mixed)
            else:
                raise RuntimeError("Can't find LV for: %s" % mixed)

        @classmethod
        def find_by_tag(cls, tag):
            lvs = LVM._vgs(["--noheadings", "--ignoreskippedcluster",
                            "@%s" % tag, "-o", "lv_full_name"])
            return [cls.from_lvm_name(lv.strip())
                    for lv in lvs.splitlines()]

        @classmethod
        def from_tag(cls, tag):
            lvs = cls.find_by_tag(tag)
            assert len(lvs) == 1
            return lvs[0]

        @classmethod
        def from_lvm_name(cls, lvm_name):
            """Easy way to get an opbject for the lvm name

            >>> lv = LVM.LV.from_lvm_name("HostVG/Foo")
            >>> lv.vg_name
            'HostVG'
            >>> lv.lv_name
            'Foo'
            """
            return cls.from_lv_name(*lvm_name.split("/"))

        @classmethod
        def from_path(cls, path):
            """Get an object for the path
            """
            data = LVM._lvs(["--noheadings", "--ignoreskippedcluster",
                             "-ovg_name,lv_name", path])
            data = data.strip()
            assert data, "Failed to find LV for path: %s" % path
            log.debug("Found LV for path %s: %s" % (path, data))
            assert len(data.splitlines()) == 1
            return cls.from_lv_name(*shlex.split(data))

        def create_snapshot(self, new_name):
            assert LVM.is_name_valid(new_name)
            LVM._lvcreate(["--snapshot",
                           "--name", new_name,
                           self.lvm_name])
            vol = LVM.LV.from_lv_name(self.vg_name, new_name)
            return LVM.register_volume(vol)

        def remove(self, force=False):
            cmd = ["-ff"] if force else []
            cmd.append(self.lvm_name)
            LVM._lvremove(cmd)

        def rename(self, new_name):
            LVM._lvrename([self.vg_name, self.lv_name, new_name])
            self.lv_name = new_name

        def activate(self, val, ignoreactivationskip=False):
            assert val in [True, False]
            val = "y" if val else "n"
            cmd = ["--activate", val, self.lvm_name]
            if ignoreactivationskip:
                cmd.append("--ignoreactivationskip")
            LVM._lvchange(cmd)

        def setactivationskip(self, val):
            assert val in [True, False]
            val = "y" if val else "n"
            LVM._lvchange(["--setactivationskip", val,
                           self.lvm_name])

        def permission(self, val):
            assert val in ["r", "rw"]
            attr = val if val == "r" else "w"
            perm = LVM._lvs(["--noheadings", "-oattr", self.lvm_name])[1]
            if perm == attr:
                return
            LVM._lvchange(["--permission", val, self.lvm_name])

        def thinpool(self):
            pool_lv = LVM._lvs(["--noheadings", "--ignoreskippedcluster",
                                "-opool_lv", self.lvm_name])
            lv = None
            if pool_lv:
                lv = LVM.LV.from_lv_name(self.vg_name, pool_lv)

            if lv is None:
                raise MissingLvmThinPool()

            return lv

        def deltag(self, tag):
            LVM._lvchange(["--deltag", tag, self.lvm_name])

        def addtag(self, tag):
            LVM._lvchange(["--addtag", tag, self.lvm_name])

        def tags(self):
            return LVM._lvs(["--noheadings", "--ignoreskippedcluster",
                             "-olv_tags", self.lvm_name]).split(",")

        def origin(self):
            lv_name = self.options(["origin"]).pop()
            return LVM.LV.from_lv_name(self.vg_name, lv_name)

        def profile(self):
            return self.options(["lv_profile"]).pop()

        def set_profile(self, name, config=None):
            args = ["--config", config] if config else []
            LVM._lvchange(args + ["--metadataprofile", name, self.lvm_name])

        def options(self, options):
            sep = "$"
            cmd = ["--noheadings",
                   "--ignoreskippedcluster",
                   "--separator", sep,
                   "-o", ",".join(options),
                   self.lvm_name]
            return LVM._lvs(cmd).strip().split(sep)

        def protect(self):
            self.permission("r")
            self.setactivationskip(True)
            self.activate(False, True)

        def unprotect(self):
            self.permission("rw")
            self.setactivationskip(False)
            self.activate(True, True)

        def unprotected(self):
            this = self

            class UnprotectedBase(object):
                obj = this

                def __enter__(self):
                    self.obj.unprotect()

                def __exit__(self, exc_type, exc_value, tb):
                    self.obj.protect()
            return UnprotectedBase()

    class Thinpool(LV):
        def create_thinvol(self, vol_name, volsize):
            assert LVM.is_name_valid(vol_name)
            vol = LVM.LV.from_lv_name(self.vg_name, vol_name)
            LVM._lvcreate(["--thin",
                           "--virtualsize", volsize,
                           "--name", vol.lv_name,
                           self.lvm_name])
            return LVM.register_volume(vol)

        def _get_metadata_size(self):
            args = ["--noheadings", "--ignoreskippedcluster", "--nosuffix",
                    "--units", "m", "-o", "metadata_percent,lv_metadata_size",
                    self.lvm_name]
            return map(float, LVM._lvs(args).split())

        def _resize_metadata(self, x_size_mb):
            free = float(LVM._vgs(["--noheading", "--ignoreskippedcluster",
                                   "--nosuffix", "-o", "free",
                                   "--units", "m", self.vg_name]))
            if x_size_mb <= free:
                args = ["--poolmetadatasize", "+{}m".format(x_size_mb),
                        self.lvm_name]
                LVM._lvextend(args)
            else:
                log.warn("Not resizing metadata: %s > %s", x_size_mb, free)

        def check_metadata_size(self, resize=False):
            min_size_mb = 1024
            meta_pct, meta_sz = self._get_metadata_size()
            log.debug("Pool: %s, metadata size=%sM (%s%%)" % (self.lvm_name,
                                                              meta_sz,
                                                              meta_pct))
            x_size_mb = min_size_mb - meta_sz
            if x_size_mb > 0:
                if resize:
                    self._resize_metadata(x_size_mb)
                else:
                    raise ThinPoolMetadataError("Thinpool metadata too small")

# vim: sw=4 et sts=4
