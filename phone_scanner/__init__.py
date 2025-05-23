#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import json
import shlex
import sqlite3
import config
from config import logging
import pandas as pd
from collections import defaultdict
from datetime import datetime
from . import blocklist
from . import parse_dump
from .android_permissions import all_permissions
from .runcmd import catch_err, run_command

class AppScanner(object):
    device_type = ""
    # app_info_conn = dataset.connect(config.APP_INFO_SQLITE_FILE)
    app_info_conn = sqlite3.connect(
        config.APP_INFO_SQLITE_FILE.replace("sqlite:///", ""), check_same_thread=False
    )

    def __init__(self, dev_type, cli):
        assert (
            dev_type in config.DEV_SUPPRTED
        ), "dev={!r} is not supported yet. Allowed={}".format(
            dev_type, config.DEV_SUPPRTED
        )
        self.device_type = dev_type
        self.cli = cli  # The cli of the device, e.g., adb or mobiledevice
        self.ddump = None  # The dump of the device

    def setup(self):
        """If the device needs some setup to work."""
        pass

    def devices(self):
        raise Exception("Not implemented")

    def get_system_apps(self, serialno: str) -> list:
        pass

    def get_apps(self, serialno: str) -> list:
        pass

    def get_offstore_apps(self, serialno: str) -> list:
        return []

    def get_app_titles(self, serialno) -> list:
        return []

    def dump_path(self, serial) -> str:
        hmac_serial = config.hmac_serial(serial)
        fkind = 'json' if self.device_type == 'ios' else 'txt'
        return os.path.join(
            config.DUMP_DIR, f"{hmac_serial}_{self.device_type}.{fkind}"
        )

    def _load_dump(self, serialno: str) -> parse_dump.PhoneDump:
        """Load the dump file"""
        if isinstance(self.ddump, parse_dump.PhoneDump): 
            return self.ddump
        dumpf = self.dump_path(serialno)
        if not os.path.exists(dumpf):
            self._dump_phone(serialno)
        try:
            if self.device_type == "android":
                self.ddump = parse_dump.AndroidDump(dumpf)
            elif self.device_type == "ios":
                self.ddump = parse_dump.IosDump(dumpf)
        except FileNotFoundError as e:
            logging.error(f"Error opening dump file: {e}")

    def _dump_phone(self, serial: str) -> bool:
        dumpf = self.dump_path(serial)
        print(f"DUMPING phone {self.device_type} INFO...")
        q = run_command(
            "bash {script} {ser} {dump_file}",
            script=f"scripts/{self.device_type}_scan.sh",
            cli=self.cli,
            ser=serial,
            dump_file=dumpf,
            nowait=False,
        )
        if q.returncode is not None and q.returncode > 0:
            logging.error(
                ">> Phone ({self.cmd}) dump failed with returncode={}. ~~ ex={!r}".format(
                    self.cmd, q, q.stderr.read() + q.stdout.read()
                )
            )
        else:
            logging.info("Phone dumping success!")
            return True
    
    def app_details(self, serialno, appid) -> tuple[dict, dict]:
        if not self.ddump:
            self._load_dump(serialno)
        try:
            d = pd.read_sql(
                "select * from apps where appid=?", self.app_info_conn, params=(appid,)
            )
            if not isinstance(d.get("permissions", ""), list):
                d["permissions"] = d.get("permissions", pd.Series([]))
                d["permissions"] = d["permissions"].fillna("").str.split(", ")
            if "descriptionHTML" not in d:
                d["descriptionHTML"] = d["description"]
            info = self.ddump.info(appid)

            config.logging.info("BEGIN APP INFO")
            config.logging.info("info={}".format(info))
            config.logging.info("END APP INFO")
            # FIXME: sloppy iOS hack but should fix later, just add these to DF
            # directly.
            if self.device_type == "ios":
                # TODO: add extra info about iOS? Like idevicediagnostics
                # ioregentry AppleARMPMUCharger or IOPMPowerSource or
                # AppleSmartBattery.
                d["permissions"] = pd.Series(info.get("permissions", ""), dtype=object)
                d["title"] = pd.Series(info.get("title", ""))
                del info["permissions"]
            d = d.fillna("").to_dict(orient="index").get(0, {})
            return d, info
        except KeyError as ex:
            logging.error(">>> Exception:::", ex)
            return dict(), dict()

    def find_spyapps(self, serialno: str):
        """Finds the apps in the phone and add flags to them based on @blocklist.py
        Return the sorted dataframe
        This is the **main** function that is called from the views in web/view/scan.py
        """
        installed_apps = self.get_apps(serialno)

        if len(installed_apps) <= 0:
            return pd.DataFrame(
                [], columns=["title", "flags", "score", "class_", "html_flags"]
            )
        r = blocklist.app_title_and_flag(
            pd.DataFrame({"appId": installed_apps}),
            offstore_apps=self.get_offstore_apps(serialno),
            system_apps=self.get_system_apps(serialno),
        )
        r["title"] = r.title.fillna("")
        if self.device_type == "android":
            td = pd.read_sql(
                "select appid as appId, title from apps where appid in (?{})".format(
                    ", ?" * (len(installed_apps) - 1)
                ),
                self.app_info_conn,
                params=(installed_apps),
                index_col="appId",
            )
            td.index.rename("appId", inplace=True)
        elif self.device_type == "ios":
            td = self.get_app_titles(serialno)

        r.set_index("appId", inplace=True)
        r.loc[td.index, "title"] = td.get("title", "")
        r.reset_index(inplace=True)

        r["class_"] = r["flags"].apply(blocklist.assign_class)
        r["score"] = r["flags"].apply(blocklist.score)
        r["title"] = r.title.str.encode("ascii", errors="ignore").str.decode("ascii")
        r["title"] = r.title.fillna("")
        r["html_flags"] = r["flags"].apply(blocklist.flag_str)
        r.sort_values(
            by=["score", "appId"],
            ascending=[False, True],
            inplace=True,
            na_position="last",
        )
        r.set_index("appId", inplace=True)

        return r[["title", "flags", "score", "class_", "html_flags"]]

    def flag_apps(self, serialno):
        installed_apps = self.get_apps(serialno)
        app_flags = blocklist.flag_apps(installed_apps)
        return app_flags

    def uninstall(self, serial, appid):
        pass

    def save(self, table, **kwargs):
        # try:
        #     tab = db.get_table(table)
        #     kwargs['device'] = kwargs.get('device', self.device_type)
        #     tab.insert(kwargs)
        #     db.commit()
        #     return True
        # except Exception as ex:
        #     print(">> Exception:", ex, file=sys.stderr)
        #     return False
        return False

    def device_info(self, serial):
        return "Test Phone", {}

    def isrooted(self, serial):
        return (False, [])


class AndroidScanner(AppScanner):
    """NEED Android Debug Bridge (adb) tool installed. Ensure your Android device
    is connected through Developer Mode with USB Debugging enabled, and `adb
    devices` showing the device as connected before running this scan function.

    """

    def __init__(self):
        super(AndroidScanner, self).__init__("android", config.ADB_PATH)
        self.installed_apps = None

    def setup(self):
        p = run_command("{cli} kill-server; {cli} start-server", cli=self.cli)
        if p != 0:
            logging.error(
                ">> Setup failed with returncode={}. ~~ ex={!r}".format(
                    p.returncode, p.stderr.read() + p.stdout.read()
                ),
                file=sys.stderr,
            )

    def _get_apps_from_device(self, serialno, flag) -> list:
        """get apps from the device"""
        logging.info("DO NOT USE THIS FUNCTION! Please")
        cmd = "{cli} -s {serial} shell pm list packages {flag} | sed 's/^package://g' | sort"
        s = catch_err(
            run_command(cmd, cli=self.cli, serial=serialno, flag=flag),
            msg="App search failed",
            cmd=cmd,
        )
        if not s:
            self.setup()
            return []
        else:
            installed_apps = [x for x in s.splitlines() if x]
            return installed_apps

    def _get_apps_from_dump(self, serialno:str) -> list:
        self._load_dump(serialno)
        if not self.ddump:
            logging.error("Could not load the dump file.")
            return []
        return self.ddump.all_apps()

    def get_apps(self, serialno: str) -> list:
        """Get apps from the Android file dump"""
        logging.info(f"Getting Android apps for serialno={serialno} from dump.")
        #   installed_apps = self._get_apps_from_device(serialno, "-u")
        installed_apps = self._get_apps_from_dump(serialno=serialno)
        self.installed_apps = installed_apps
        return installed_apps

    def get_system_apps(self, serialno: str) -> list:
        """Get system apps from the Android file dump"""
        return self.ddump.system_apps()

    def get_offstore_apps(self, serialno: str) -> list:
        """Get offstore apps from the Android file dump"""
        return self.ddump.offstore_apps()

    def devices(self):
        # FIXME: check for errors related to err in runcmd.py.
        # cmd = '{cli} devices | tail -n +2 | cut -f2'
        # runcmd = catch_err(run_command(cmd), cmd=cmd).strip()
        # cmd = '{cli} kill-server; {cli} start-server'
        # s = catch_err(run_command(cmd), time=30, msg="ADB connection failed", cmd=cmd)
        cmd = "{cli} devices | tail -n +2"
        runcmd = catch_err(run_command(cmd, cli=self.cli), cmd=cmd).strip().split("\n")
        conn_devices = []
        for rc in runcmd:
            d = rc.split()
            if len(d) != 2:
                continue
            device, state = rc.split()
            device = device.strip()
            if state.strip() == "device":
                conn_devices.append(device)
        return conn_devices

    # def devices_info(self):
    #     cmd = '{cli} devices -l'
    #     return run_command(cmd).stdout.read().decode('utf-8')

    def device_info(self, serial):
        m = {}
        cmd = "{cli} -s {serial} shell getprop ro.product.brand"
        m["brand"] = (
            run_command(cmd, cli=self.cli, serial=serial).stdout.read().decode("utf-8").title()
        )

        cmd = "{cli} -s {serial} shell getprop ro.product.model"
        m["model"] = run_command(cmd, cli=self.cli, serial=serial).stdout.read().decode("utf-8")

        cmd = "{cli} -s {serial} shell getprop ro.build.version.release"
        m["version"] = (
            run_command(cmd, cli=self.cli, serial=serial).stdout.read().decode("utf-8").strip()
        )

        cmd = '{cli} -s {serial} shell dumpsys batterystats | grep -i "Start clock time:" | head -n1'
        # runcmd = catch_err(run_command(cmd, serial=serial), cmd=cmd)
        # m['last_full_charge'] = datetime.strptime(runcmd.split(':')[1].strip(), '%Y-%m-%d-%H-%M-%S')
        m["last_full_charge"] = datetime.now()
        return "{brand} {model} (running Android {version})".format(**m), m

    # def dump_phone(self, serialno=None):
    #     if not serialno:
    #         serialno = self.devices()[0]
    #     cmd = '{cli} -s {serial} shell dumpsys'
    #     p = run_command(cmd, serial=serialno)
    #     outfname = os.path.join(config.DUMP_DIR, '{}.txt.gz'.format(serialno))
    #     # if p.returncode != 0:
    #     #     print("Dump command failed")
    #     #     return
    #     with gzip.open(outfname, 'w') as f:
    #         f.write(p.stdout.read())
    #     print("Dump success! Written to={}".format(outfname))

    def uninstall(self, serial, appid):
        cmd = "{cli} uninstall {appid!r}"
        s = catch_err(
            run_command(cmd, cli=self.cli, appid=shlex.quote(appid)),
            cmd=cmd,
            msg="Could not uninstall",
        )
        return s != -1

    def app_details(self, serialno, appid) -> tuple[dict, dict]:
        d, info = super(AndroidScanner, self).app_details(serialno, appid)
        # part that requires android to be connected / store this somehow.
        hf_recent, non_hf_recent, non_hf, stats = all_permissions(
            self.dump_path(serialno), appid
        )
        # FIXME: some appopps in non_hf_recent are not included in the
        # output.  maybe concat hf_recent with them?
        info["Date of Scan"] = datetime.now().strftime(config.DATE_STR)
        info["Installation Date"] = stats.get("firstInstallTime", "")
        info["Last Updated"] = stats.get("lastUpdateTime", "")
        # info['Last Used'] = stats['used']

        # TODO: what is the difference between usedScr and used?  Does a
        # background process count as used? Probably not since appOps
        # permissions have been more recent than 'used' on some scans.
        # info['Last Used Screen'] = stats['usedScr']
        info["App Version"] = stats.get("versionName", "")
        # info['App Version Code'] = stats['versionCode']

        # FIXME: if Unknown, use 'permission_abbrv' instead.
        hf_recent.loc[hf_recent["label"] == "unknown", "label"] = hf_recent.get(
            "permission_abbrv", ""
        )

        # hf_recent['label'] = hf_recent[['label',
        # 'timestamp']].apply(lambda x: ''.join(str(x), axis=1))

        if len(hf_recent.get("label", "")) > 0:
            hf_recent["label"] = hf_recent.apply(
                lambda x: "{} (last used: {})".format(
                    x["label"],
                    "never" if "unknown" in x["timestamp"].lower() else x["timestamp"],
                ),
                axis=1,
            )
        # print("hf_recent['label']=", hf_recent['label'].tolist())
        # print(~hf_recent['timestamp'].str.contains('unknown'))
        non_hf_recent.drop("appId", axis=1, inplace=True)
        print(d)
        d["permissions"] = hf_recent["label"].tolist()
        d["non_hf_permissions_html"] = non_hf_recent.to_html()
        print("App info dict:", d)

        # hf_recent['label'] = hf_recent['label'].map(str) + " (last used by app: "+\
        #        (hf_recent['timestamp'].map(str) if isinstance(hf_recent['timestamp'], datetime) else 'nooo') +")"
        # d['recent_permissions'] = hf_recent['timestamp']
        # print(d['recent_permissions'])
        return d, info

    def isrooted(self, serial):
        """
        Doesn't return all reasons by default. First match will return.
        TODO: make consistent with iOS isrooted, which returns all reasons discovered.
        """
        # FIXME: load these from a private database instead.  from OWASP,
        # https://sushi2k.gitbooks.io/the-owasp-mobile-security-testing-guide/content/0x05j-Testing-Resiliency-Against-Reverse-Engineering.html

        root_pkgs_check_str = "\\|".join(
            [
                "com.noshufou.android.su",
                "com.thirdparty.superuser",
                "eu.chainfire.supersu",
                "com.koushikdutta.superuser",
                "com.zachspong.temprootremovejb",
                "com.ramdroid.appquarantine",
            ]
        )
        print(root_pkgs_check_str)
        root_checks = {
            "su binary": ("command -v su", "0"),
            "oem unlock": ("getprop ro.boot.flash.locked", "0"),
            "frida server": ("ps -A | grep frida", "0"),
            "root_pkgs": (f"pm list packages | grep {root_pkgs_check_str}", "0"),
        }
        for k, v in root_checks.items():
            cmd = "{cli} -s {serial} shell '{v[0]}'"
            s = catch_err(run_command(cmd, cli=self.cli, serial=shlex.quote(serial), v=v))
            if s.strip() == v[1]:
                return (True, f"The device is rooted: Found:  {k!r}.")
        return (False, "The device is probably not rooted.")


class IosScanner(AppScanner):
    def __init__(self):
        super(IosScanner, self).__init__("ios", cli=config.LIBIMOBILEDEVICE_PATH)
        self.installed_apps = None

    # TODO: This might send titles out of order. Fix this to send both appid and
    # titles.
    def get_app_titles(self, serialno):
        if not self.ddump:
            self._dump_phone(serialno)
        return self.ddump.installed_apps_titles()

    def get_apps(self, serialno: str) -> list:
        """iOS always read everything from dump, so nothing to change."""
        if not self._dump_phone(serialno):
            print("Failed to dump the phone. Check error on the terminal")
            return []
        self._load_dump(serialno)
        self.installed_apps = self.ddump.installed_apps()
        print("iOS INFO DUMPED.")
        return self.installed_apps

    def get_system_apps(self, serialno: str) -> list:
        if self.ddump:
            return self.ddump.system_apps()
        else:
            return []

    def devices(self):
        def _is_device(x):
            """Is it looks like a serial number"""
            return re.match(r"[a-f0-9]+", x) is not None

        # cmd = "{}idevice_id -l | tail -n 1".format(self.cli)
        
        cmd = "{cli} usbmux list"
        s = catch_err(run_command(cmd, cli=self.cli), cmd=cmd, msg="")
        try:
            d = [a.get("Identifier", "") for a in json.loads(s)]
        except json.JSONDecodeError as e:
            print(f">>> ERROR: {e!r}", file=sys.stderr)
            d = []
        logging.info(f"Devices found: {d}")
        return d

    def device_info(self, serialno):
        dumped = self._dump_phone(serialno)
        self._load_dump(serialno)
        if dumped:
            device_info_print, device_info_map = self.ddump.device_info()
            return (device_info_print, device_info_map)
        else:
            return ("", {})


    def uninstall(self, serial, appid):
        # cmd = '{self.cli} -i {serial} --uninstall_only --bundle_id {appid!r}'
        # cmd = 'ideviceinstaller --udid {} --uninstall {appid!r}'.format(serial, appid)
        cmd = "{cli} apps uninstall {appid!r}"
        s = catch_err(run_command(cmd, cli=self.cli, appid=appid), cmd=cmd, msg="Could not uninstall")
        return s != -1

    def isrooted(self, serial):
        # dict with 'True' and 'False' mapping to a list of reasons for root/no root
        rooted = defaultdict(list)
        # TODO This should be removed once the check is fixed
        rooted["False"].append("Jailbreak and root checks are currently " "disabled")
        return (False, rooted["False"])
        try:
            with open(self.dump_path(serial, "Jailbroken-FS"), "r") as fh:
                JAILBROKEN_LOG = fh.readlines()
            if (
                "Your device needs to be jailbroken and have the AFC2 service installed.\n"
                in JAILBROKEN_LOG
            ):
                rooted["False"].append(
                    "Filesystem is not rooted. *Highly unlikely* to be jailbroken."
                )
            elif "No such file or directory" in JAILBROKEN_LOG:
                rooted["False"].append("Unable to check device.")
            else:
                rooted["True"].append(
                    "Filesystem *might* be rooted. Conduct additional checks."
                )
        except FileNotFoundError:
            print("Couldn't find Jailbroken FS check log.")
            # TODO: trigger error message? like
            # TODO: show a try again, maybe it's not plugged in properly. still not working?
            # this could be due to many many many reasons.
            # return (True, ['FS check failed, jailbreak not necessarily occurring.'])

        try:
            with open(self.dump_path(serial, "Jailbroken-SSH"), "r") as fh:
                JAILBROKEN_SSH_LOG = fh.readlines()
            if "0\n" in JAILBROKEN_SSH_LOG:
                rooted["True"].append("SSH is enabled.")
        except FileNotFoundError:
            # TODO: trigger error message? like
            # TODO: show a try again, maybe it's not plugged in properly. still not working?
            #  this could be due to many many many reasons.
            print("Couldn't find Jailbroken SSH check log.")

        # if app["Path"].split("/")[-1] in ["Cydia.app"]
        """ Summary of jailbroken detection: checks for commonly installed jailbreak apps,
        tries to mount root filesystem (AFC2, by default on iOS 7 and lower,
        tries to SSH into the phone (FIXME). iproxy 2222 22 `idevice_id -l` says
        "waiting for connection" perpertually if not work. says "accepted connection" on next line if it does.
        https://twitter.com/bellis1000/status/807527492810665984?lang=en
        # add to jailbroken log
        # FIXME: load from private data blocklist. More to be added.
        """
        # FIXME: NEED to apply first to df. self.installed_apps not sufficient.
        #  dotapps.append(app["Path"].split("/")[-1])

        apps_titles = self.parse_dump.installed_apps_titles()["title"].tolist()
        # TODO: convert to set check
        for app in [
            "Cydia",
            "blackra1n",
            "Undecimus",
            "FakeCarrier",
            "Icy",
            "IntelliScreen",
            "MxTube",
            "RockApp",
            "SBSettings",
            "WinterBoard",
            "3uTools",
            "Absinthe",
            "backr00m",
            "blackra1n",
            "Corona",
            "doubleH3lix",
            "Electra",
            "EtasonJB",
            "evasi0n",
            "evasi0n7",
            "G0blin",
            "Geeksn0w",
            "greenpois0n",
            "h3lix",
            "Home Depot",
            "ipwndfu",
            "JailbreakMe",
            "LiberiOS",
            "LiberTV",
            "limera1n",
            "Meridian",
            "p0sixspwn",
            "Pangu",
            "Pangu8",
            "Pangu9",
            "Phœnix",
            "PPJailbreak",
            "purplera1n",
            "PwnageTool",
            "redsn0w",
            "RockyRacoon",
            "Rocky Racoon",
            "Saïgon",
            "Seas0nPass",
            "sn0wbreeze",
            "Spirit",
            "TaiG",
            "unthredera1n",
            "yalu",
        ]:
            if app in apps_titles:
                rooted["True"].append("{} was found on the device.".format(app))

        # if apps check passes
        if not rooted:
            rooted["False"].append("Did not find popular jailbreak apps installed.")
            """ check for jailbroken status after attempts logged by ios_dump.sh """
        if "True" in rooted:
            return (True, rooted["True"])
        else:
            return (False, rooted["False"])


class TestScanner(AppScanner):
    def __init__(self):
        super(TestScanner, self).__init__("android", cli="cli")

    def get_apps(self, serialno):
        # assert serialno == 'testdevice1'
        installed_apps = open(config.TEST_APP_LIST, "r").read().splitlines()
        return installed_apps

    def devices(self):
        return ["testdevice1", "testdevice2"]

    def get_system_apps(self, serialno):
        return self.get_apps(serialno)[:10]

    def get_offstore_apps(self, serialno):
        return self.get_apps(serialno)[-4:]

    def uninstall(self, serial, appid):
        return True
