"""
gas_db.py

Google Apps Script database client for JuiceFront.
All communication happens via POST requests with JSON.

Requirements:
    pip install requests
"""

import os
import requests

GAS_URL = os.getenv(
    "GAS_URL",
    "https://script.google.com/macros/s/AKfycbwA0NkSdSjRBglN8ZR-rSmmE732uR45_G0ZRzxOSVrSZnz4bTEOR2R86brSBG2PshZz/exec"
)


class GasDB:

    def __init__(self, base_url=GAS_URL):
        self.base_url = base_url.rstrip("/")

    def post(self, action, **payload):

        payload["action"] = action

        response = requests.post(
            self.base_url,
            json=payload,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("success", False):
            raise Exception(
                data.get(
                    "message",
                    "Google Apps Script request failed."
                )
            )

        return data


db = GasDB()

# =====================================================
# PUBLIC
# =====================================================

def get_vendors():
    return db.post("getVendors")["vendors"]


def get_all_juices():
    return db.post("getJuices")["juices"]


def get_juices(vendor_id):
    return db.post(
        "getJuices",
        vendorId=vendor_id
    )["juices"]


def place_order(order):
    return db.post(
        "placeOrder",
        **order
    )


# =====================================================
# VENDOR
# =====================================================

def vendor_login(username, password):
    return db.post(
        "vendorLogin",
        username=username,
        password=password
    )


def get_vendor_orders(vendor_id):
    return db.post(
        "getVendorOrders",
        vendorId=vendor_id
    )["orders"]


def update_order_status(order_id, status):
    return db.post(
        "updateOrderStatus",
        orderId=order_id,
        status=status
    )


# =====================================================
# ADMIN
# =====================================================

def add_vendor(vendor):
    return db.post(
        "addVendor",
        **vendor
    )


def add_juice(juice):
    return db.post(
        "addJuice",
        **juice
    )


def get_all_orders():
    return db.post(
        "getOrders"
    )["orders"]

# -----------------------------------------------------
# Compatibility wrappers
# -----------------------------------------------------

def vendor_orders(vendor_id):
    return get_vendor_orders(vendor_id)


def all_orders():
    return get_all_orders()


def get_vendor(vendor_id):
    vendors = get_vendors()

    for vendor in vendors:
        if str(vendor["vendorId"]) == str(vendor_id):
            return vendor

    return None