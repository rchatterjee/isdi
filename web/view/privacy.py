from flask import request, render_template
from web import app
from web.view import get_device
from phone_scanner.privacy_scan_android import do_privacy_check
import config


@app.route("/privacy", methods=["GET"])
def privacy():
    """
    TODO: Privacy scan. Think how should it flow.
    Privacy is a seperate page.
    """
    return render_template(
        "main.html",
        task="privacy",
        device_primary_user=config.DEVICE_PRIMARY_USER,
        title=config.TITLE,
    )


@app.route("/privacy/<device>/<cmd>", methods=["GET"])
def privacy_scan(device, cmd):
    sc = get_device(device)
    res = do_privacy_check(sc.serialno, cmd)
    return res
