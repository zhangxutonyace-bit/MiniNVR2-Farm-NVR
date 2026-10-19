"""Curated data-only model catalog. No remote code, arbitrary URLs or PT files."""
from dataclasses import asdict, dataclass
import math
import json
from pathlib import Path
import sys

from .person_localizer import MODEL_BYTES, MODEL_NAME, MODEL_SHA256


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    filename: str
    bytes: int
    sha256: str
    profile: str
    input_size: int
    url: str
    source: str
    license_url: str
    note: str
    bundled: bool = False
    license: str = "Apache-2.0"


_ZOO = "47534e27c9851bb1128ccc0102f1145e27f23f98"
_YOLOX = "6ddff4824372906469a7fae2dc3206c7aa4bbaee"
MODELS = (
    ModelSpec("nanodet-coco", "NanoDet COCO（内置轻量）", MODEL_NAME,
              MODEL_BYTES, MODEL_SHA256, "nanodet-rgb416", 416, "",
              "https://github.com/opencv/opencv_zoo/tree/25f423d0e04c31a17254620e58febd7386da523b/models/object_detection_nanodet",
              "https://github.com/opencv/opencv_zoo/blob/25f423d0e04c31a17254620e58febd7386da523b/models/object_detection_nanodet/LICENSE",
              "无需下载；使用现有 OpenCV CPU 推理。", True),
    ModelSpec("yolox-nano", "YOLOX Nano（轻量）", "yolox_nano.onnx", 3659407,
              "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d",
              "yolox-bgr416", 416,
              "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx",
              "https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0",
              "https://github.com/Megvii-BaseDetection/YOLOX/blob/" + _YOLOX + "/LICENSE",
              "适合先在旧电脑上试用；首次使用需下载。"),
    ModelSpec("yolox-s", "YOLOX-S（较大）", "object_detection_yolox_2022nov.onnx", 35858002,
              "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
              "yolox-rgb640", 640,
              "https://media.githubusercontent.com/media/opencv/opencv_zoo/" + _ZOO + "/models/object_detection_yolox/object_detection_yolox_2022nov.onnx",
              "https://github.com/opencv/opencv_zoo/tree/" + _ZOO + "/models/object_detection_yolox",
              "https://github.com/opencv/opencv_zoo/blob/" + _ZOO + "/models/object_detection_yolox/LICENSE",
              "CPU 开销高于轻量模型；多路或低性能电脑可能出现无框回退。"),
)
MODEL_BY_ID = {m.id: m for m in MODELS}
COCO_CLASSES = tuple((
    "person,bicycle,car,motorcycle,airplane,bus,train,truck,boat,traffic light,fire hydrant,"
    "stop sign,parking meter,bench,bird,cat,dog,horse,sheep,cow,elephant,bear,zebra,giraffe,"
    "backpack,umbrella,handbag,tie,suitcase,frisbee,skis,snowboard,sports ball,kite,baseball bat,"
    "baseball glove,skateboard,surfboard,tennis racket,bottle,wine glass,cup,fork,knife,spoon,"
    "bowl,banana,apple,sandwich,orange,broccoli,carrot,hot dog,pizza,donut,cake,chair,couch,"
    "potted plant,bed,dining table,toilet,tv,laptop,mouse,remote,keyboard,cell phone,microwave,"
    "oven,toaster,sink,refrigerator,book,clock,vase,scissors,teddy bear,hair drier,toothbrush"
).split(","))
# The UI has 14 switches covering the 15 requested names; stool/chair merge.
CAPABILITIES = (
    ("person", "人形", (0,)), ("car", "汽车", (2,)),
    ("seat", "座椅（椅子/凳子）", (56,)), ("table", "桌子（餐桌类别）", (60,)),
    ("cat", "猫", (15,)), ("dog", "狗", (16,)), ("pets", "宠物（猫和狗）", (15, 16)),
    ("phone", "手机", (67,)), ("sofa", "沙发", (57,)), ("bed", "床", (59,)),
    ("tv", "电视", (62,)), ("laptop", "笔记本电脑", (63,)),
    ("cup", "杯子", (41,)), ("bottle", "瓶子", (39,)),
)
CAPABILITY_IDS = {entry[0] for entry in CAPABILITIES}
CLASS_STYLES = {
    0: ("行人", "#F51616"), 2: ("汽车", "#E86D08"),
    56: ("座椅", "#E86D08"), 60: ("餐桌", "#91C20D"),
    15: ("猫", "#0931C0"), 16: ("狗", "#91C20D"),
    67: ("手机", "#F51616"), 57: ("沙发", "#E86D08"),
    59: ("床", "#0931C0"), 62: ("电视", "#91C20D"),
    63: ("笔记本电脑", "#F51616"), 41: ("杯子", "#E86D08"), 39: ("瓶子", "#0931C0"),
}
CLASS_LABELS = {cid: style[0] for cid, style in CLASS_STYLES.items()}
DEFAULT_OPTIONS = {"model_id": "nanodet-coco", "targets": ["person"], "confidence": .35}


def model_spec(model_id):
    if not isinstance(model_id, str) or model_id not in MODEL_BY_ID:
        raise ValueError("请选择商城中支持的算法模型")
    return MODEL_BY_ID[model_id]


def validate_options(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_OPTIONS):
        raise ValueError("算法设置只能包含模型、识别目标和置信度")
    model_id = value.get("model_id", DEFAULT_OPTIONS["model_id"])
    model_spec(model_id)
    targets = value.get("targets", DEFAULT_OPTIONS["targets"])
    if (not isinstance(targets, list) or not targets or len(targets) > len(CAPABILITIES)
            or any(not isinstance(x, str) or x not in CAPABILITY_IDS for x in targets)):
        raise ValueError("请至少勾选一个商城支持的识别目标")
    threshold = value.get("confidence", .35)
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold) or not .1 <= threshold <= .95):
        raise ValueError("置信度应为 0.10 至 0.95 之间的数字")
    return {"model_id": model_id, "targets": sorted(set(targets)), "confidence": float(threshold)}


def selected_classes(options):
    wanted = set(options["targets"])
    return frozenset(cid for key, _, classes in CAPABILITIES if key in wanted for cid in classes)


def validate_persisted_options(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_OPTIONS):
        raise ValueError("已保存的算法设置无效，请到商城重新选择并应用")
    return validate_options(value)


def revision_for(options):
    o = validate_options(options)
    return (o["model_id"], model_spec(o["model_id"]).sha256, tuple(o["targets"]), o["confidence"])


def public_catalog():
    return [dict(asdict(model), class_map=list(COCO_CLASSES)) for model in MODELS]


def category_styles(classes):
    return [{"class_id": cid, "label": CLASS_STYLES[cid][0], "color": CLASS_STYLES[cid][1]}
            for cid in classes]


def public_capabilities():
    return [{"id": key, "name": name, "classes": list(classes), "styles": category_styles(classes)}
            for key, name, classes in CAPABILITIES]


def catalog_document():
    return {"models": public_catalog(),
            "capabilities": public_capabilities(),
            "category_styles": category_styles(CLASS_STYLES)}


def verify_catalog_asset():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    path = root / "third_party" / "AlgorithmCatalog" / "catalog.json"
    if not path.is_file() or json.loads(path.read_text(encoding="utf-8")) != catalog_document():
        raise RuntimeError("算法目录资源与固定适配器不匹配")
    return path
