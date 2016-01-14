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
import shlex
import logging
from .utils import LvmCLI


log = logging.getLogger(__package__)


class LVM(object):
    _lvs = LvmCLI.lvs
    _vgs = LvmCLI.vgs
    _lvcreate = LvmCLI.lvcreate
    _lvchange = LvmCLI.lvchange
    _lvremove = LvmCLI.lvremove
    _vgcreate = LvmCLI.vgcreate
    _vgchange = LvmCLI.vgchange

    @staticmethod
    def list_lv_names():
        cmd = ["--noheadings", "-o", "lv_name"]
        raw = LVM._lvs(cmd)
        lvs = [n.strip() for n in raw.splitlines()]
        return sorted(lvs)

    class VG(object):
        vg_name = None

        def __init__(self, vg_name):
            self.vg_name = vg_name

        def __repr__(self):
            return "<VG '%s' />" % self.vg_name

        @staticmethod
        def from_vg_name(vg_name):
            vg = LVM.VG()
            vg.vg_name = vg_name
            return vg

        @staticmethod
        def find_by_tag(tag):
            vgs = LVM._vgs(["--noheadings", "--select",
                            "vg_tags = %s" % tag, "-o", "vg_name"])
            return [LVM.VG(vg_name.strip()) for vg_name in vgs.splitlines()]

        @staticmethod
        def from_tag(tag):
            vgs = LVM.VG.find_by_tag(tag)
            assert len(vgs) == 1
            return vgs[0]

        @staticmethod
        def create(vg_name, pv_paths):
            LVM._vgcreate([vg_name] + pv_paths)
            return LVM.VG(vg_name)

        def addtag(self, tag):
            LVM._vgchange(["--addtag", tag, self.vg_name])

        def tags(self):
            return LVM._vgs(["--noheadings", "-ovg_tags",
                            self.vg_name]).split(",")

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
            return LVM._lvs(["--noheadings", "-olv_path", self.lvm_name])

        @staticmethod
        def from_lv_name(vg_name, lv_name):
            lv = LVM.LV()
            lv.vg_name = vg_name
            lv.lv_name = lv_name
            return lv

        def __repr__(self):
            return "<LV '%s' />" % self.lvm_name

        @staticmethod
        def try_find(mixed):
            log.debug("Trying to find LV for: %s" % mixed)
            assert mixed
            if mixed.startswith("/dev"):
                return LVM.LV.from_path(mixed)
            elif "/" in mixed:
                return LVM.LV.from_lvm_name(mixed)
            elif "@" in mixed:
                return LVM.LV.from_tag(mixed)
            else:
                raise RuntimeError("Can't find LV for: %s" % mixed)

        @staticmethod
        def find_by_tag(tag):
            lvs = LVM._vgs(["--noheadings", "@%s" % tag,
                            "-o", "lv_full_name"])
            return [LVM.LV.from_lvm_name(lv.strip())
                    for lv in lvs.splitlines()]

        @staticmethod
        def from_tag(tag):
            lvs = LVM.LV.find_by_tag(tag)
            assert len(lvs) == 1
            return lvs[0]

        @staticmethod
        def from_lvm_name(lvm_name):
            """Easy way to get an opbject for the lvm name

            >>> lv = LVM.LV.from_lvm_name("HostVG/Foo")
            >>> lv.vg_name
            'HostVG'
            >>> lv.lv_name
            'Foo'
            """
            return LVM.LV.from_lv_name(*lvm_name.split("/"))

        @staticmethod
        def from_path(path):
            """Get an object for the path
            """
            data = LVM._lvs(["--noheadings", "-ovg_name,lv_name", path])
            data = data.strip()
            assert data, "Failed to find LV for path: %s" % path
            log.debug("Found LV for path %s: %s" % (path, data))
            assert len(data.splitlines()) == 1
            return LVM.LV.from_lv_name(*shlex.split(data))

        def create_snapshot(self, new_name):
            LVM._lvcreate(["--snapshot",
                           "--name", new_name,
                           self.lvm_name])
            return LVM.LV.from_lv_name(self.vg_name, new_name)

        def remove(self, force=False):
            cmd = ["-f"] if force else []
            cmd.append(self.lvm_name)
            LVM._lvremove(cmd)

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
            LVM._lvchange(["--permission", val,
                           self.lvm_name])

        def thinpool(self):
            pool_lv = LVM._lvs(["--noheadings", "-opool_lv",
                               self.lvm_name])
            lv = None
            if pool_lv:
                lv = LVM.LV.from_lv_name(self.vg_name, pool_lv)
            return lv

        def addtag(self, tag):
            LVM._lvchange(["--addtag", tag, self.lvm_name])

        def tags(self):
            return LVM._lvs(["--noheadings", "-olv_tags",
                            self.lvm_name]).split(",")

        def origin(self):
            lv_name = self.options(["origin"]).pop()
            return LVM.LV.from_lv_name(self.vg_name, lv_name)

        def options(self, options):
            sep = "$"
            cmd = ["--noheadings",
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
            vol = LVM.LV.from_lv_name(self.vg_name, vol_name)
            LVM._lvcreate(["--thin",
                           "--virtualsize", volsize,
                           "--name", vol.lv_name,
                           self.lvm_name])
            return vol

# vim: sw=4 et sts=4
