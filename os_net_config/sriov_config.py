# -*- coding: utf-8 -*-

# Copyright 2014 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

#
# The sriov_config.py module does the SR-IOV PF configuration.
# It'll be invoked by the sriov_config systemd service for the persistence of
# the SR-IOV configuration across reboots. And os-net-config:utils also invokes
# it for the first time configuration.
# An entry point os-net-config-sriov is added for invocation of this module.

import argparse
import os
import pyudev
import queue
import re
import sys
import time

from json import loads
from os_net_config import common
from os_net_config import sriov_bind_config
from oslo_concurrency import processutils

logger = common.configure_logger()

_UDEV_RULE_FILE = '/etc/udev/rules.d/80-persistent-os-net-config.rules'
_UDEV_LEGACY_RULE_FILE = '/etc/udev/rules.d/70-os-net-config-sriov.rules'
_IFUP_LOCAL_FILE = '/sbin/ifup-local'
_RESET_SRIOV_RULES_FILE = '/etc/udev/rules.d/70-tripleo-reset-sriov.rules'
_ALLOCATE_VFS_FILE = '/etc/sysconfig/allocate_vfs'
_MLNX_DRIVER = "mlx5_core"
MLNX_UNBIND_FILE_PATH = "/sys/bus/pci/drivers/mlx5_core/unbind"
MLNX5_VDPA_KMODS = [
    "vdpa",
    "vhost_vdpa",
    "mlx5_vdpa",
]

MAX_RETRIES = 10
PF_FUNC_RE = re.compile(r"\.(\d+)$", 0)

VF_PCI_RE = re.compile(r'/[\d]{4}\:(\d+):(\d+)\.(\d+)/net/[^\/]+$')
# In order to keep VF representor name consistent specially after the upgrade
# proccess, we should have a udev rule to handle that.
# The udev rule will rename the VF representor as "<sriov_pf_name>_<vf_num>"
_REP_LINK_NAME_FILE = "/etc/udev/rep-link-name.sh"
_REP_LINK_NAME_DATA = '''#!/bin/bash
# This file is autogenerated by os-net-config
set -x
PORT="$1"
echo "NUMBER=${PORT##pf*vf}"
'''

# Create a queue for passing the udev network events
vf_queue = queue.Queue()


# Global variable to store link between pci/pf
# for udev rule creationg when dealing with mlnx vdpa
vf_to_pf = {}


class SRIOVNumvfsException(ValueError):
    pass


def udev_event_handler(action, device):
    event = {"action": action, "device": device.sys_path}
    logger.info(
        f"Received udev event {event['action']} for {event['device']}"
    )
    vf_queue.put(event)


def _norm_path(dev, suffix):
    return os.path.normpath(os.path.join(dev, suffix))


def _get_pf_path(device):
    pf_path = _norm_path(device, "../../physfn/net")
    if not os.path.isdir(pf_path):
        pf_path = _norm_path(device, "physfn/net")
        if not os.path.isdir(pf_path):
            pf_path = None
    return pf_path


def _driver_unbind(dev):
    vf_pci_path = f"/sys/bus/pci/devices/{dev}/driver"
    if os.path.exists(vf_pci_path):
        logger.info(f"{dev}: Unbinding driver")
        with open(MLNX_UNBIND_FILE_PATH, 'w') as f:
            f.write(dev)
    else:
        logger.info(f"{dev}: No driver to unbind")


def _wait_for_vf_creation(pf_name, numvfs):
    vf_count = 0
    pf_config = common.get_sriov_map(pf_name)
    vdpa = False
    if len(pf_config):
        vdpa = pf_config[0].get('vdpa', False)
    while vf_count < numvfs:
        try:
            # wait for 5 seconds after every udev event
            event = vf_queue.get(True, 5)
            vf_name = os.path.basename(event["device"])
            pf_path = _get_pf_path(event["device"])
            logger.debug(f"{event['device']}: Got udev event: {event}")
            if pf_path:
                pf_nic = os.listdir(pf_path)
                # NOTE(dvd): For vDPA devices, the VF event we're interrested
                # in contains all the VFs. We can also use this to build a dict
                # to correlate the VF pci address to the PF when creating the
                # vdpa representator udev rule
                #
                # Data structure sample for vDPA:
                # pf_path:
                #   /sys/devices/pci0000:00/0000:00:02.2/0000:06:01.2/physfn/net
                # pf_nic: ['enp6s0f1np1_0', 'enp6s0f1np1_1', 'enp6s0f1np1']
                # pf_name: enp6s0f1np1
                if vf_name not in vf_to_pf and pf_name in pf_nic:
                    vf_to_pf[vf_name] = {
                        'device': event['device'],
                        'pf': pf_name
                    }
                    logger.info(
                        f"{pf_name}: VF {vf_name} created"
                    )
                    vf_count += 1
                elif vf_name in vf_to_pf:
                    logger.debug(
                        f"{pf_name}: VF {vf_name} was already created"
                    )
                elif vdpa:
                    logger.warning(f"{pf_name}: This PF is not in {pf_path}")
                else:
                    logger.warning(
                        f"{pf_name}: Unable to parse event {event['device']}"
                    )
            elif not vdpa:
                logger.warning(f"{event['device']}: Unable to find PF")
        except queue.Empty:
            logger.info(f"{pf_name}: Timeout in the creation of VFs")
            return
    logger.info(f"{pf_name}: Required VFs are created")


def get_numvfs(ifname):
    """Getting sriov_numvfs for PF

    Wrapper that will get the sriov_numvfs file for a PF.

    :param ifname: interface name (ie: p1p1)
    :returns: int -- the number of current VFs on ifname
    :raises: SRIOVNumvfsException
    """
    sriov_numvfs_path = common.get_dev_path(ifname, "sriov_numvfs")
    logger.debug(f"{ifname}: Getting numvfs for interface")
    try:
        with open(sriov_numvfs_path, 'r') as f:
            curr_numvfs = int(f.read())
    except IOError as exc:
        msg = f"{ifname}: Unable to read numvfs: {exc}"
        raise SRIOVNumvfsException(msg)
    logger.debug(f"{ifname}: Interface has {curr_numvfs} configured")
    return curr_numvfs


def set_numvfs(ifname, numvfs):
    """Setting sriov_numvfs for PF

    Wrapper that will set the sriov_numvfs file for a PF.

    After numvfs has been set for an interface, _wait_for_vf_creation will be
    called to monitor the creation.

    Some restrictions:
    - if current number of VF is already defined, we can't change numvfs
    - if sriov_numvfs doesn't exist for an interface, we can't create it

    :param ifname: interface name (ie: p1p1)
    :param numvfs: an int that represents the number of VFs to be created.
    :returns: int -- the number of current VFs on ifname
    :raises: SRIOVNumvfsException
    """
    curr_numvfs = get_numvfs(ifname)
    logger.debug(f"{ifname}: Interface has {curr_numvfs} configured, setting "
                 f"to {numvfs}")
    if not isinstance(numvfs, int):
        msg = (f"{ifname}: Unable to configure pf with numvfs: {numvfs}\n"
               f"numvfs must be an integer")
        raise SRIOVNumvfsException(msg)

    if numvfs != curr_numvfs:
        if curr_numvfs != 0:
            logger.warning(f"{ifname}: Numvfs already configured to "
                           f"{curr_numvfs}")
            return curr_numvfs

        sriov_numvfs_path = common.get_dev_path(ifname, "sriov_numvfs")
        try:
            logger.debug(f"Setting {sriov_numvfs_path} to {numvfs}")
            with open(sriov_numvfs_path, "w") as f:
                f.write("%d" % numvfs)
        except IOError as exc:
            msg = (f"{ifname} Unable to configure pf  with numvfs: {numvfs}\n"
                   f"{exc}")
            raise SRIOVNumvfsException(msg)

        _wait_for_vf_creation(ifname, numvfs)
        curr_numvfs = get_numvfs(ifname)
        if curr_numvfs != numvfs:
            msg = (f"{ifname}: Unable to configure pf with numvfs: {numvfs}\n"
                   "sriov_numvfs file is not set to the targeted number of "
                   "vfs")
            raise SRIOVNumvfsException(msg)
    return curr_numvfs


def restart_ovs_and_pfs_netdevs():
    sriov_map = common.get_sriov_map()
    processutils.execute('/usr/bin/systemctl', 'restart', 'openvswitch')
    for item in sriov_map:
        if item['device_type'] == 'pf':
            if_down_interface(item['name'])
            if_up_interface(item['name'])


def cleanup_puppet_config():
    file_contents = ""
    if os.path.exists(_RESET_SRIOV_RULES_FILE):
        os.remove(_RESET_SRIOV_RULES_FILE)
    if os.path.exists(_ALLOCATE_VFS_FILE):
        os.remove(_ALLOCATE_VFS_FILE)
    if os.path.exists(_IFUP_LOCAL_FILE):
        # Remove the invocation of allocate_vfs script generated by puppet
        # After the removal of allocate_vfs, if the ifup-local file has just
        # "#!/bin/bash" left, then remove the file as well.
        with open(_IFUP_LOCAL_FILE) as oldfile:
            for line in oldfile:
                if "/etc/sysconfig/allocate_vfs" not in line:
                    file_contents = file_contents + line
        if file_contents.strip() == "#!/bin/bash":
            os.remove(_IFUP_LOCAL_FILE)
        else:
            with open(_IFUP_LOCAL_FILE, 'w') as newfile:
                newfile.write(file_contents)


def udev_monitor_setup():
    # Create a context for pyudev and observe udev events for network
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by('net')
    observer = pyudev.MonitorObserver(monitor, udev_event_handler)
    return observer


def udev_monitor_start(observer):
    observer.start()


def udev_monitor_stop(observer):
    observer.stop()


def is_partitioned_pf(dev_name: str) -> bool:
    """Check if any nic-partition(VF) is already used

    Given a PF device, returns True if any VFs of this
    device are in-use.
    """
    sriov_map = common.get_sriov_map()
    for config in sriov_map:
        devtype = config.get('device_type', None)
        if devtype == 'vf':
            name = config.get('device', {}).get('name')
            vf_name = config.get('name')
            if dev_name == name:
                logger.warning(f"{name} has VF({vf_name}) used by host")
                return True
    return False


def configure_sriov_pf(execution_from_cli=False, restart_openvswitch=False):
    observer = udev_monitor_setup()
    udev_monitor_start(observer)

    sriov_map = common.get_sriov_map()
    dpdk_vfs_pcis_list = []
    trigger_udev_rule = False

    # Cleanup the previous config by puppet-tripleo
    cleanup_puppet_config()
    if any(item.get('vdpa') for item in sriov_map):
        common.load_kmods(MLNX5_VDPA_KMODS)
        vdpa_devices = get_vdpa_vhost_devices()

    for item in sriov_map:
        if item['device_type'] == 'pf':
            if pf_configure_status(item):
                logger.debug(f"PF {item['name']} is already configured")
                continue
            _pf_interface_up(item)
            if item.get('link_mode') == "legacy":
                # Add a udev rule to configure the VF's when PF's are
                # released by a guest
                if not is_partitioned_pf(item['name']):
                    add_udev_rule_for_legacy_sriov_pf(item['name'],
                                                      item['numvfs'])
            # When configuring vdpa, we need to configure switchdev before
            # we create the VFs
            is_mlnx = common.is_mellanox_interface(item['name'])
            vdpa = item.get('vdpa')
            # Configure switchdev mode when vdpa
            # It has to happen before we set_numvfs
            if vdpa and is_mlnx:
                configure_switchdev(item['name'])
            set_numvfs(item['name'], item['numvfs'])
            # Configure switchdev, unbind driver and configure vdpa
            if item.get('link_mode') == "switchdev" and is_mlnx:
                logger.info(f"{item['name']}: Mellanox card")
                vf_pcis_list = get_vf_pcis_list(item['name'])
                for vf_pci in vf_pcis_list:
                    if not vdpa:
                        # For DPDK, we need to unbind the driver
                        _driver_unbind(vf_pci)
                    else:
                        if vf_pci not in vdpa_devices:
                            configure_vdpa_vhost_device(vf_pci)
                        else:
                            logger.info(
                                f"{item['name']}: vDPA device already created "
                                f"for {vf_pci}"
                            )
                if vdpa:
                    common.restorecon('/dev/vhost-*')
                logger.info(f"{item['name']}: Adding udev rules")
                # Adding a udev rule to make vf-representors unmanaged by
                # NetworkManager
                add_udev_rule_to_unmanage_vf_representors_by_nm()

                # Adding a udev rule to save the sriov_pf name
                trigger_udev_rule = add_udev_rule_for_sriov_pf(item['name'])\
                    or trigger_udev_rule

                # Adding a udev rule to rename vf-representors
                trigger_udev_rule = add_udev_rule_for_vf_representors(
                    item['name']) or trigger_udev_rule

                if not vdpa:
                    # This is used for the sriov_bind_config
                    dpdk_vfs_pcis_list += vf_pcis_list

                    # Configure flow steering mode, default to smfs
                    configure_flow_steering(item['name'],
                                            item.get('steering_mode', 'smfs'))

                    # Configure switchdev mode
                    configure_switchdev(item['name'])
                else:
                    trigger_udev_rule = add_udev_rule_for_vdpa_representors(
                        item['name']) or trigger_udev_rule

                # Moving the sriov-PFs to switchdev mode will put the netdev
                # interfaces in down state.
                # In case we are running during initial deployment,
                # bring the interfaces up.
                # In case we are running as part of the sriov_config service
                # after reboot, net config scripts, which run after
                # sriov_config service will bring the interfaces up.
                if execution_from_cli:
                    if_up_interface(item['name'])

    if dpdk_vfs_pcis_list and not vdpa:
        sriov_bind_pcis_map = {_MLNX_DRIVER: dpdk_vfs_pcis_list}
        if not execution_from_cli:
            sriov_bind_config.update_sriov_bind_pcis_map(sriov_bind_pcis_map)
        else:
            sriov_bind_config.configure_sriov_bind_service()
            sriov_bind_config.bind_vfs(sriov_bind_pcis_map)

    # Trigger udev rules if there is new rules written
    if trigger_udev_rule:
        trigger_udev_rules()

    udev_monitor_stop(observer)
    if restart_openvswitch:
        restart_ovs_and_pfs_netdevs()


def _wait_for_uplink_rep_creation(pf_name):
    uplink_rep_phys_switch_id_path = f"/sys/class/net/{pf_name}/phys_switch_id"

    for i in range(MAX_RETRIES):
        if common.get_file_data(uplink_rep_phys_switch_id_path):
            logger.info(f"{pf_name} Uplink representor ready")
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"{pf_name}: Timeout waiting uplink representor")


def create_rep_link_name_script():
    with open(_REP_LINK_NAME_FILE, "w") as f:
        f.write(_REP_LINK_NAME_DATA)
    # Make the _REP_LINK_NAME_FILE executable
    os.chmod(_REP_LINK_NAME_FILE, 0o755)


def add_udev_rule_for_sriov_pf(pf_name):
    logger.info(f"{pf_name}: adding udev rules for sriov")
    pf_pci = get_pf_pci(pf_name)
    udev_data_line = 'SUBSYSTEM=="net", ACTION=="add", DRIVERS=="?*", '\
                     f'KERNELS=="{pf_pci}", NAME="{pf_name}"'
    return add_udev_rule(udev_data_line, _UDEV_RULE_FILE)


def add_udev_rule_for_legacy_sriov_pf(pf_name, numvfs):
    logger.info(f"{pf_name}: adding udev rules for legacy sriov: {numvfs}")
    udev_line = f'KERNEL=="{pf_name}", '\
                f'RUN+="/bin/os-net-config-sriov -n %k:{numvfs}"'
    pattern = f'KERNEL=="{pf_name}", RUN+="/bin/os-net-config-sriov -n'
    return add_udev_rule(udev_line, _UDEV_LEGACY_RULE_FILE, pattern)


def add_udev_rule_for_vf_representors(pf_name):
    logger.info(f"{pf_name}: adding udev rules for vf representators")
    phys_switch_id_path = common.get_dev_path(pf_name,
                                              "_phys_switch_id")
    phys_switch_id = common.get_file_data(phys_switch_id_path).strip()
    pf_pci = get_pf_pci(pf_name)
    pf_fun_num_match = PF_FUNC_RE.search(pf_pci)
    if not pf_fun_num_match:
        logger.error(f"{pf_name}: Failed to get function number "
                     "and so failed to create a udev rule for renaming "
                     "its vf-represent")
        return

    pf_fun_num = pf_fun_num_match.group(1)
    udev_data_line = 'SUBSYSTEM=="net", ACTION=="add", ATTR{phys_switch_id}'\
                     '=="%s", ATTR{phys_port_name}=="pf%svf*", '\
                     'IMPORT{program}="%s $attr{phys_port_name}", '\
                     'NAME="%s_$env{NUMBER}"' % (phys_switch_id,
                                                 pf_fun_num,
                                                 _REP_LINK_NAME_FILE,
                                                 pf_name)
    create_rep_link_name_script()
    return add_udev_rule(udev_data_line, _UDEV_RULE_FILE)


def add_udev_rule_for_vdpa_representors(pf_name):
    logger.info(f"{pf_name}: adding udev rules for vdpa representators")
    udev_lines = ""
    for vf, att in vf_to_pf.items():
        mac = common.interface_mac(vf)
        vadd = VF_PCI_RE.search(att.get('device'))
        if not vadd:
            logger.error(
                f"{att.get('device')}/{vf}: Failed to get pf/vf numbers "
                "and so failed to create a udev rule for renaming vdpa dev"
            )
            continue
        vdpa_rep = f"vdpa{vadd.group(1)}p{vadd.group(2)}vf{vadd.group(3)}"
        logger.info(f"{vdpa_rep}: Adding udev representor rule.")
        udev_lines += (
            'SUBSYSTEM=="net", ACTION=="add", '
            f'ATTR{{address}}=="{mac}", NAME="{vdpa_rep}"\n'
        )
    return add_udev_rule(udev_lines, _UDEV_RULE_FILE)


def add_udev_rule_to_unmanage_vf_representors_by_nm():
    logger.info("adding udev rules to unmanage vf representators")
    udev_data_line = 'SUBSYSTEM=="net", ACTION=="add", ATTR{phys_switch_id}'\
                     '!="", ATTR{phys_port_name}=="pf*vf*", '\
                     'ENV{NM_UNMANAGED}="1"'
    return add_udev_rule(udev_data_line, _UDEV_RULE_FILE)


def add_udev_rule(udev_data, udev_file, pattern=None):
    logger.debug(f"adding udev rule to {udev_file}: {udev_data}")
    trigger_udev_rule = False
    udev_data = udev_data.strip()
    if not pattern:
        pattern = udev_data
    if not os.path.exists(udev_file):
        with open(udev_file, "w") as f:
            data = "# This file is autogenerated by os-net-config\n"\
                   f"{udev_data}\n"
            f.write(data)
    else:
        file_data = common.get_file_data(udev_file)
        udev_lines = file_data.splitlines()
        if pattern in file_data:
            if udev_data in udev_lines:
                return trigger_udev_rule
            with open(udev_file, "w") as f:
                for line in udev_lines:
                    if pattern in line:
                        f.write(udev_data + "\n")
                    else:
                        f.write(line + "\n")
        else:
            with open(udev_file, "a") as f:
                f.write(udev_data + "\n")

    reload_udev_rules()
    trigger_udev_rule = True
    return trigger_udev_rule


def reload_udev_rules():
    try:
        processutils.execute('/usr/sbin/udevadm', 'control', '--reload-rules')
        logger.info("udev rules reloaded successfully")
    except processutils.ProcessExecutionError as exc:
        logger.error(f"Failed to reload udev rules: {exc}")
        raise


def trigger_udev_rules():
    try:
        processutils.execute('/usr/sbin/udevadm', 'trigger', '--action=add',
                             '--attr-match=subsystem=net')
        logger.info("udev rules triggered successfully")
    except processutils.ProcessExecutionError as exc:
        logger.error(f"Failed to trigger udev rules: {exc}")
        raise


def configure_switchdev(pf_name):
    pf_pci = get_pf_pci(pf_name)
    pf_device_id = get_pf_device_id(pf_name)
    if pf_device_id == "0x1013" or pf_device_id == "0x1015":
        try:
            processutils.execute('/usr/sbin/devlink', 'dev', 'eswitch', 'set',
                                 f'pci/{pf_pci}', 'inline-mode', 'transport')
        except processutils.ProcessExecutionError as exc:
            logger.error(f"{pf_name}: Failed to set inline-mode to transport "
                         f"for {pf_pci}: {exc}")
            raise
    try:
        processutils.execute('/usr/sbin/devlink', 'dev', 'eswitch', 'set',
                             f'pci/{pf_pci}', 'mode', 'switchdev')
    except processutils.ProcessExecutionError as exc:
        logger.error(f"{pf_name}: Failed to set mode to switchdev for "
                     f"{pf_pci}: {exc}")
        raise
    logger.info(f"{pf_name}: Device pci/{pf_pci} set to switchdev mode.")

    # WA to make sure that the uplink_rep is ready after moving to switchdev,
    # as moving to switchdev will remove the sriov_pf and create uplink
    # representor, so we need to make sure that uplink representor is ready
    # before proceed
    _wait_for_uplink_rep_creation(pf_name)

    try:
        processutils.execute('/usr/sbin/ethtool', '-K', pf_name,
                             'hw-tc-offload', 'on')
        logger.info(f"{pf_name}: Enabled \"hw-tc-offload\" for PF.")
    except processutils.ProcessExecutionError as exc:
        logger.error(f"{pf_name}: Failed to enable hw-tc-offload: {exc}")
        raise


def get_vdpa_vhost_devices():
    logger.info(f"Getting list of vdpa devices")
    try:
        stdout, stderr = processutils.execute('vdpa', '-j', 'dev')
    except processutils.ProcessExecutionError as exc:
        logger.error(f"Failed to get vdpa vhost devices: {exc}")
        raise
    return loads(stdout)['dev']


def configure_vdpa_vhost_device(pci):
    logger.info(f"{pci}: Creating vdpa device")
    try:
        processutils.execute('vdpa', 'dev', 'add', 'name', pci,
                             'mgmtdev', f'pci/{pci}')
    except processutils.ProcessExecutionError as exc:
        logger.error(f"{pci}: Failed to create vdpa vhost device: {exc}")
        raise


def configure_flow_steering(pf_name, steering_mode):
    pf_pci = get_pf_pci(pf_name)
    try:
        processutils.execute('/usr/sbin/devlink', 'dev', 'param', 'set',
                             f'pci/{pf_pci}', 'name', 'flow_steering_mode',
                             'value', steering_mode, 'cmode', 'runtime')
        logger.info(f"{pf_name}: Device pci/{pf_pci} is set to"
                    f" {steering_mode} steering mode.")
    except processutils.ProcessExecutionError as exc:
        logger.warning(f"{pf_name}: Could not set pci/{pf_pci} to"
                       f" {steering_mode} steering mode: {exc}")


def run_ip_config_cmd(*cmd, **kwargs):
    logger.info("Running %s" % ' '.join(cmd))
    try:
        processutils.execute(*cmd, delay_on_retry=True, attempts=10, **kwargs)
    except processutils.ProcessExecutionError as exc:
        logger.error("Failed to execute %s: %s" % (' '.join(cmd), exc))
        raise


def _pf_interface_up(pf_device):
    if 'promisc' in pf_device:
        run_ip_config_cmd('ip', 'link', 'set', 'dev', pf_device['name'],
                          'promisc', pf_device['promisc'])
    logger.info(f"{pf_device['name']}: Bringing up PF")
    run_ip_config_cmd('ip', 'link', 'set', 'dev', pf_device['name'], 'up')


def pf_configure_status(pf_device):
    return pf_device['numvfs'] == get_numvfs(pf_device['name'])


def run_ip_config_cmd_safe(raise_error, *cmd, **kwargs):
    try:
        run_ip_config_cmd(*cmd)
    except processutils.ProcessExecutionError:
        if raise_error:
            raise


def get_pf_pci(pf_name):
    pf_pci_path = common.get_dev_path(pf_name, "uevent")
    pf_info = common.get_file_data(pf_pci_path)
    pf_pci = re.search(r'PCI_SLOT_NAME=(.*)', pf_info, re.MULTILINE).group(1)
    return pf_pci


def get_pf_device_id(pf_name):
    pf_device_path = common.get_dev_path(pf_name, "device")
    pf_device_id = common.get_file_data(pf_device_path).strip()
    return pf_device_id


def get_vf_pcis_list(pf_name):
    vf_pcis_list = []
    pf_files = os.listdir(common.get_dev_path(pf_name, "_device"))
    for pf_file in pf_files:
        if pf_file.startswith("virtfn"):
            vf_info = common.get_file_data(common.get_dev_path(pf_name,
                                           f"{pf_file}/uevent"))
            vf_pcis_list.append(re.search(r'PCI_SLOT_NAME=(.*)',
                                          vf_info, re.MULTILINE).group(1))
    return vf_pcis_list


def if_down_interface(device):
    logger.info(f"{device}: Running /sbin/ifdown")
    try:
        processutils.execute('/sbin/ifdown', device)
    except processutils.ProcessExecutionError:
        logger.error(f"{device}: Failed to ifdown")
        raise


def if_up_interface(device):
    logger.info(f"{device}: Running /sbin/ifup")
    try:
        processutils.execute('/sbin/ifup', device)
    except processutils.ProcessExecutionError:
        logger.error(f"{device}: Failed to ifup")
        raise


def configure_sriov_vf():
    sriov_map = common.get_sriov_map()
    for item in sriov_map:
        raise_error = True
        if item['device_type'] == 'vf':
            pf_name = item['device']['name']
            vfid = item['device']['vfid']
            base_cmd = ('ip', 'link', 'set', 'dev', pf_name, 'vf', str(vfid))
            logger.info(f"{pf_name}: Configuring settings for VF: {vfid} "
                        f"VF name: {item['name']}")
            raise_error = True
            if 'macaddr' in item:
                cmd = base_cmd + ('mac', item['macaddr'])
                run_ip_config_cmd(*cmd)
            if 'vlan_id' in item:
                vlan_cmd = base_cmd + ('vlan', str(item['vlan_id']))
                if 'qos' in item:
                    vlan_cmd = vlan_cmd + ('qos', str(item['qos']))
                run_ip_config_cmd(*vlan_cmd)
            if 'max_tx_rate' in item:
                cmd = base_cmd + ('max_tx_rate', str(item['max_tx_rate']))
                if item['max_tx_rate'] == 0:
                    raise_error = False
                run_ip_config_cmd_safe(raise_error, *cmd)
            if 'min_tx_rate' in item:
                cmd = base_cmd + ('min_tx_rate', str(item['min_tx_rate']))
                if item['min_tx_rate'] == 0:
                    raise_error = False
                run_ip_config_cmd_safe(raise_error, *cmd)
            if 'spoofcheck' in item:
                cmd = base_cmd + ('spoofchk', item['spoofcheck'])
                run_ip_config_cmd(*cmd)
            if 'state' in item:
                cmd = base_cmd + ('state', item['state'])
                run_ip_config_cmd(*cmd)
            if 'trust' in item:
                cmd = base_cmd + ('trust', item['trust'])
                run_ip_config_cmd(*cmd)
            if 'promisc' in item:
                run_ip_config_cmd('ip', 'link', 'set', 'dev', item['name'],
                                  'promisc', item['promisc'])
            if 'driver' in item:
                common.set_driverctl_override(item['pci_address'],
                                              item['driver'])


def parse_opts(argv):

    parser = argparse.ArgumentParser(
        description='Configure SR-IOV PF and VF interfaces using a YAML'
        ' config file format.')

    parser.add_argument(
        '-d', '--debug',
        dest="debug",
        action='store_true',
        help="Print debugging output.",
        required=False)

    parser.add_argument(
        '-v', '--verbose',
        dest="verbose",
        action='store_true',
        help="Print verbose output.",
        required=False)

    parser.add_argument(
        '-n', '--numvfs',
        dest="numvfs",
        action='store',
        help="Provide the numvfs for device in the format <device>:<numvfs>",
        required=False)

    opts = parser.parse_args(argv[1:])

    return opts


def main(argv=sys.argv, main_logger=None):
    opts = parse_opts(argv)
    if not main_logger:
        main_logger = common.configure_logger(log_file=True)
    common.logger_level(main_logger, opts.verbose, opts.debug)

    if opts.numvfs:
        if re.match(r"^\w+:\d+$", opts.numvfs):
            device_name, numvfs = opts.numvfs.split(':')
            set_numvfs(device_name, int(numvfs))
        else:
            main_logger.error(f"Invalid arguments for --numvfs {opts.numvfs}")
            return 1
    else:
        # Configure the PF's
        configure_sriov_pf()
        # Configure the VFs
        configure_sriov_vf()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
