"""
retry_oci.py
────────────────────────────────────────────────────────────────
Thử tạo Oracle Ampere A1.Flex instance (2 OCPU / 12GB).
Thiết kế để chạy qua GitHub Actions theo lịch (mỗi 15 phút).

- Nếu instance A1.Flex đã tồn tại (RUNNING/PROVISIONING) → không làm gì, thoát.
- Nếu chưa có, thử launch 1 lần:
    - Thành công → in thông tin, thoát.
    - Lỗi "Out of capacity" → đây là điều BÌNH THƯỜNG, thoát với mã 0
      (để không làm workflow báo đỏ liên tục, tránh gây nhiễu).
    - Lỗi khác (auth sai, config sai...) → thoát với mã 1 để bạn để ý.
"""

import os
import sys
import time
import urllib.request
import urllib.parse
import oci


def notify_telegram(text):
    """Gửi thông báo Telegram — chỉ gọi khi THÀNH CÔNG. Lỗi ở đây không làm crash script."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("⚠️  Chưa cấu hình Telegram secrets, bỏ qua bước thông báo.")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        print("📩 Đã gửi thông báo Telegram.")
    except Exception as e:
        print(f"⚠️  Gửi Telegram thất bại (không ảnh hưởng kết quả chính): {e}")

# Danh sách các shape muốn thử tạo, theo thứ tự ưu tiên.
# Mỗi lần chạy sẽ thử LẦN LƯỢT từng cái — cái nào đã có máy rồi thì bỏ qua,
# cái nào chưa có thì thử tạo.
CONFIGS = [
    {
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 2,
        "memory_gb": 12,
        "os_version_must_contain": "aarch64",
        "display_name": "main-server-auto",
    },
    {
        "shape": "VM.Standard.E2.1.Micro",
        "ocpus": None,          # shape cố định, không cần custom OCPU/RAM
        "memory_gb": None,
        "os_version_must_contain": None,   # không được chứa "aarch64" (x86)
        "display_name": "micro-server-auto",
    },
]


def build_config():
    key_path = "/tmp/oci_api_key.pem"
    with open(key_path, "w") as f:
        f.write(os.environ["OCI_PRIVATE_KEY"])
    os.chmod(key_path, 0o600)

    return {
        "user": os.environ["OCI_USER_OCID"],
        "fingerprint": os.environ["OCI_FINGERPRINT"],
        "tenancy": os.environ["OCI_TENANCY_OCID"],
        "region": os.environ["OCI_REGION"],
        "key_file": key_path,
    }


def already_has_instance(compute_client, compartment_id, shape):
    instances = compute_client.list_instances(compartment_id=compartment_id).data
    for inst in instances:
        if inst.shape == shape and inst.lifecycle_state in (
            "RUNNING", "PROVISIONING", "STARTING"
        ):
            print(f"✅ [{shape}] Đã có instance rồi: {inst.display_name} ({inst.lifecycle_state}) — bỏ qua.")
            return True
    return False


def find_image_id(compute_client, compartment_id, shape, must_contain):
    images = compute_client.list_images(
        compartment_id=compartment_id,
        operating_system="Canonical Ubuntu",
        shape=shape,
    ).data
    for img in images:
        version = img.operating_system_version or ""
        if must_contain:
            if "24.04 Minimal" in version and must_contain in version:
                return img.id
        else:
            if "24.04 Minimal" in version and "aarch64" not in version:
                return img.id
    if images:
        print(f"⚠️  [{shape}] Không thấy đúng bản 24.04 Minimal mong muốn, dùng tạm: {images[0].display_name}")
        return images[0].id
    raise RuntimeError(f"Không tìm thấy Image nào tương thích với shape {shape}")


def try_launch(compute_client, compartment_id, cfg):
    shape = cfg["shape"]

    if already_has_instance(compute_client, compartment_id, shape):
        return

    image_id = find_image_id(compute_client, compartment_id, shape, cfg["os_version_must_contain"])

    kwargs = dict(
        availability_domain=os.environ["OCI_AVAILABILITY_DOMAIN"],
        compartment_id=compartment_id,
        shape=shape,
        display_name=cfg["display_name"],
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image_id),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=os.environ["OCI_SUBNET_ID"],
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": os.environ["OCI_SSH_PUBLIC_KEY"]},
    )

    # Chỉ shape "Flex" mới cần/được phép truyền shape_config
    if cfg["ocpus"] is not None:
        kwargs["shape_config"] = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=cfg["ocpus"],
            memory_in_gbs=cfg["memory_gb"],
        )

    launch_details = oci.core.models.LaunchInstanceDetails(**kwargs)

    try:
        result = compute_client.launch_instance(launch_details)
        print(f"🎉 [{shape}] THÀNH CÔNG! Instance đã được tạo:")
        print(f"   Name: {result.data.display_name}")
        print(f"   State: {result.data.lifecycle_state}")
        print(f"   OCID: {result.data.id}")
        print("👉 Vào Oracle Console → Compute → Instances để xem, đợi 1-2 phút để chuyển sang Running.")
        notify_telegram(
            f"🎉 Đã tạo thành công Oracle instance!\n"
            f"Shape: {shape}\n"
            f"Name: {result.data.display_name}\n"
            f"State: {result.data.lifecycle_state}\n"
            f"Vào Oracle Console để kiểm tra nhé."
        )
    except oci.exceptions.ServiceError as e:
        msg = str(e.message or "")
        if "Out of host capacity" in msg or "Out of capacity" in msg:
            print(f"⏳ [{shape}] Hết chỗ trống thật sự (Out of capacity) — bình thường, thử lại sau.")
        elif "Too many requests" in msg or e.status == 429:
            print(f"🚦 [{shape}] Bị Oracle giới hạn tần suất (rate-limit) — KHÔNG rõ có chỗ trống hay không, thử lại sau.")
        else:
            print(f"❌ [{shape}] Lỗi thật cần bạn kiểm tra lại config/secrets: {msg}")
            global HAD_REAL_ERROR
            HAD_REAL_ERROR = True


HAD_REAL_ERROR = False


def main():
    config = build_config()
    compute_client = oci.core.ComputeClient(config)
    compartment_id = config["tenancy"]

    for i, cfg in enumerate(CONFIGS):
        if i > 0:
            time.sleep(5)   # nghỉ giữa 2 lần gọi API để giảm khả năng bị rate-limit
        try_launch(compute_client, compartment_id, cfg)

    sys.exit(1 if HAD_REAL_ERROR else 0)


if __name__ == "__main__":
    main()
