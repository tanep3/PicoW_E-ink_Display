"""Copy to config.py and adapt non-secret Pico LAN settings locally.

HOST/PORT support the original PULL program during migration.
PUSH_* and static LAN values support the new PUSH receiver.
"""

HOST = "192.168.0.120"
PORT = 16150

PUSH_PORT = 16151
SENDER_IP = "192.168.0.120"
STATIC_IP = "192.168.0.172"
SUBNET_MASK = "255.255.255.0"
GATEWAY = "192.168.0.1"
DNS_SERVER = "192.168.0.1"
