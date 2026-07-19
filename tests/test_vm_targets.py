import os
import tempfile
import unittest
from unittest import mock

from core import client_app, hwinfo, state
from core.host_server import HostServer


class VmTargetTests(unittest.TestCase):
    def test_hyperv_guest_is_detected(self):
        environment = hwinfo.classify_machine_environment(
            "Microsoft Corporation", "Virtual Machine")

        self.assertEqual("vm", environment["kind"])
        self.assertEqual("Hyper-V", environment["hypervisor"])

    def test_regular_pc_is_physical(self):
        environment = hwinfo.classify_machine_environment(
            "Micro-Star International Co., Ltd.", "MS-7D25")

        self.assertEqual("physical", environment["kind"])
        self.assertIsNone(environment["hypervisor"])

    def test_config_can_override_automatic_detection(self):
        server = HostServer.__new__(HostServer)
        server.static_info = {
            "environment": {"kind": "physical", "hypervisor": None},
        }
        server.config = {
            "node_type": "vm",
            "parent_host": "Main workstation",
        }

        identity = server.node_identity()

        self.assertEqual("vm", identity["type"])
        self.assertEqual("physical", identity["detected"])
        self.assertEqual("Main workstation", identity["parent_host"])

    def test_old_beacons_remain_physical_hosts(self):
        fields = client_app._node_fields({"name": "Legacy host"})

        self.assertEqual("physical", fields["node_type"])
        self.assertIsNone(fields["hypervisor"])

    def test_vm_beacon_metadata_is_bounded(self):
        fields = client_app._node_fields({
            "node_type": "vm",
            "node_detected": "vm",
            "hypervisor": "KVM" * 100,
            "parent_host": "Physical host",
        })

        self.assertEqual("vm", fields["node_type"])
        self.assertEqual("vm", fields["node_detected"])
        self.assertEqual(64, len(fields["hypervisor"]))
        self.assertEqual("Physical host", fields["parent_host"])

    def test_manual_vm_is_validated_and_normalized(self):
        host = client_app._manual_host({
            "name": "Gaming VM", "ip": "192.168.1.50", "port": "8532",
            "node_type": "vm", "parent_host": "Main PC",
        })

        self.assertTrue(host["manual"])
        self.assertEqual("vm", host["node_type"])
        self.assertEqual(8532, host["port"])
        self.assertEqual("Main PC", host["parent_host"])

    def test_manual_destinations_survive_restart(self):
        records = [{
            "name": "Office VM", "ip": "192.168.1.60", "port": 8532,
            "node_type": "vm", "parent_host": "Main PC",
        }]
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(state, "DATA_DIR", root), \
                mock.patch.object(state, "CLIENT_HOSTS_PATH", os.path.join(root, "hosts.json")):
            state.save_client_hosts(records)
            restored = state.load_client_hosts()

        self.assertEqual(records, restored)

    def test_invalid_manual_destination_is_rejected(self):
        with self.assertRaises(ValueError):
            client_app._manual_host({"ip": "not-an-ip", "node_type": "vm"})


if __name__ == "__main__":
    unittest.main()
