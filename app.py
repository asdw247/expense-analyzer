import base64
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from PIL import Image

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

UPLOAD_DIR = Path("./uploads")
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}
MOONSHOT_BASE_URL = "https://api.moonshot.cn/v1"
MOONSHOT_MODEL = "moonshot-v1-8k-vision-preview"
BUDGET_ADVICE_MODEL = "moonshot-v1-8k"
RECOGNITION_PROMPT = (
    "你是一个账单数据提取助手。请识别这张账单截图中的所有消费记录。"
    "每条记录提取：商户名称、消费金额、交易时间。\n\n"
    "规则：\n"
    "- 忽略转账、红包、退款、充值类交易，忽略金额带+号的交易\n"
    "- 金额用纯数字，不要带货币符号，如15.00，不要出现负数，有负号的转换为正数\n"
    "- 时间格式统一为YYYY-MM-DD HH:MM\n"
    "- 看不清的字段标注unknown，不要猜测\n"
    "- 只输出JSON数组，不要任何额外解释\n\n"
    "输出格式示例：\n"
    '[{"merchant":"商户名","amount":金额,"time":"时间"}]'
)

CATEGORY_RULES = [
    ("一日三餐", ("食堂", "饭堂", "快餐", "自选", "餐厅")),
    ("外卖", ("美团", "饿了么", "外卖")),
    (
        "奶茶咖啡零食",
        (
            "瑞幸",
            "星巴克",
            "蜜雪",
            "茶百道",
            "奶茶",
            "咖啡",
            "零食",
            "喜茶",
            "奈雪",
            "霸王茶姬",
        ),
    ),
    ("网购", ("淘宝", "京东", "拼多多", "天猫", "唯品会")),
    ("社交聚餐", ("火锅", "烧烤", "聚餐", "海底捞")),
    ("交通出行", ("滴滴", "地铁", "公交", "打车", "高德")),
    (
        "游戏娱乐",
        (
            "王者",
            "Steam",
            "游戏",
            "B站",
            "大会员",
            "腾讯视频",
            "爱奇艺",
            "网易云",
            "QQ音乐",
        ),
    ),
]

DEFAULT_ANALYSIS = {
    "comment": "本月消费数据显示，您在餐饮方面的支出占比较高，说明您比较重视日常饮食。若想进一步优化开支，不妨适当增加在食堂就餐的次数。",
    "label": "美食爱好者",
    "tips": ["可以试试一周自带餐食几天，能省下不少开支", "不妨把奶茶频次稍稍降低，既健康又省钱"],
}

DEFAULT_BUDGET_ADVICE_TIPS = [
    "不妨适当减少外卖次数，在食堂或自己做饭更实惠",
    "购物前可以先列个清单，帮您更从容地控制开支",
    "可以检查一下订阅服务，取消暂时用不到的也能省下一笔",
]

BUDGET_ADVICE_TIMEOUT = 60

BUDGET_ADVICE_PROMPT_TEMPLATE = (
    "用户本月预算{budget}元，已花{spent}元，剩余{remaining}元，"
    "最大支出类别是{top_category}。"
    "你是一位温和、关心用户的大学生财务助理。请用温和提醒、带鼓励性的语气回复："
    "先肯定剩余预算仍能支撑本月基本生活，再给出3条帮助平稳度过月底的具体小建议，每条20字以内。"
    "不要使用「紧急」「超支」等制造压力的词汇；"
    "可类似「注意到您本月花费稍快」「这里有几个小方法帮您平衡开支」。"
    "建议措辞用「可以试试」「不妨考虑」等，不说教。"
    '直接输出JSON格式：{{"tips":["建议1","建议2","建议3"]}}'
)

ANALYSIS_PROMPT_TEMPLATE = (
    "你是一位温和、专业、平易近人的大学生财务助理。以下是一位大学生本月的消费数据：\n"
    "分类统计：{summary_text}\n"
    "总消费笔数：{count}笔，总金额{total}元。\n"
    "请完成以下任务，直接输出JSON，不要加任何解释：\n"
    "1. 用温和、客观、友善的口吻写一段100字以内的消费评语："
    "先肯定用户合理的消费选择，再委婉点出可以优化的地方，有温度但不越界\n"
    "2. 根据消费数据匹配一个中性、正向的消费特征标签，从以下选择："
    "美食爱好者、外卖常客、咖啡常客、网购达人、社交小能手、节约小标兵、"
    "校园美食家、理性消费者（若都不完全匹配，可选用相近的正面描述，4-6字为宜）\n"
    "3. 给出2条具体省钱建议，结合校园消费水平（食堂人均约15元，瑞幸约9.9-15元，"
    "蜜雪冰城约3-8元，外卖约15-25元），用「可以试试」「不妨考虑」等建议性措辞，不说教\n"
    '输出格式严格为JSON：{{"comment":"评语","label":"标签","tips":["建议1","建议2"]}}'
)

# 从 .env 文件加载环境变量（如 PORT、FLASK_DEBUG 等）
load_dotenv()

# 初始化 Flask 应用
app = Flask(__name__)

# 配置 CORS，允许所有来源跨域访问
CORS(app, resources={r"/*": {"origins": "*"}})

DATABASE_PATH = "budgets.db"


def init_db():
    """初始化 SQLite 数据库，创建 budgets 表（若不存在）。"""
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                monthly_budget REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


init_db()


@app.route("/", methods=["GET"])
def index():
    """根路由：确认后端服务已运行"""
    try:
        return send_from_directory('.', 'index.html'), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def _parse_records_from_content(content: str) -> list:
    """从模型返回文本中解析 JSON 数组。"""
    text = content.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        array_match = re.search(r"\[[\s\S]*\]", text)
        if array_match:
            text = array_match.group(0)
    records = json.loads(text)
    if not isinstance(records, list):
        raise ValueError("识别结果不是 JSON 数组")
    return records


def _parse_json_object_from_content(content: str) -> dict:
    """从模型返回文本中解析 JSON 对象。"""
    text = content.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        object_match = re.search(r"\{[\s\S]*\}", text)
        if object_match:
            text = object_match.group(0)
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("解析结果不是 JSON 对象")
    return result


def _categorize_merchant(merchant: str) -> str:
    """根据商户名匹配消费类别。"""
    name = merchant or ""
    for category, keywords in CATEGORY_RULES:
        if any(keyword in name for keyword in keywords):
            return category
    return "其他消费"


def _normalize_records(records: list) -> list:
    """校验并规范化消费记录列表。"""
    if not isinstance(records, list):
        raise ValueError("请求体应为 JSON 数组")
    normalized = []
    for i, item in enumerate(records):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i + 1} 条记录格式无效")
        merchant = str(item.get("merchant", "")).strip()
        try:
            amount = float(item.get("amount", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {i + 1} 条记录金额无效") from exc
        if amount < 0:
            raise ValueError(f"第 {i + 1} 条记录金额不能为负数")
        time_str = str(item.get("time", "")).strip()
        normalized.append(
            {"merchant": merchant, "amount": round(amount, 2), "time": time_str}
        )
    return normalized


def _compute_summary(records: list):
    """统计各类别消费总额与占比（百分比保留1位小数）。"""
    totals: dict[str, float] = {}
    for record in records:
        category = _categorize_merchant(record["merchant"])
        totals[category] = totals.get(category, 0.0) + record["amount"]

    total_amount = round(sum(totals.values()), 2)
    total_count = len(records)
    summary = {}
    for category, amount in sorted(totals.items(), key=lambda x: -x[1]):
        rounded_amount = round(amount, 2)
        percentage = (
            round(rounded_amount / total_amount * 100, 1) if total_amount else 0.0
        )
        summary[category] = {"amount": rounded_amount, "percentage": percentage}
    return summary, total_amount, total_count


def _parse_record_time(time_str: str) -> Optional[datetime]:
    """解析消费时间字符串。"""
    if not time_str or time_str.lower() == "unknown":
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(time_str, fmt)
        except ValueError:
            continue
    return None


def _week_of_month(dt: datetime) -> int:
    """根据日期计算属于本月第几周（1-7日为第1周，以此类推）。"""
    return (dt.day - 1) // 7 + 1


def _compute_trend(records: list) -> dict:
    """按本月第几周汇总消费金额。"""
    weekly_totals: dict[int, float] = {}
    for record in records:
        dt = _parse_record_time(record["time"])
        if dt is None:
            continue
        week_num = _week_of_month(dt)
        weekly_totals[week_num] = weekly_totals.get(week_num, 0.0) + record["amount"]

    return {
        f"第{week}周": round(weekly_totals[week], 2)
        for week in sorted(weekly_totals)
    }


def _format_summary_text(summary: dict) -> str:
    """格式化分类统计供分析 Prompt 使用。"""
    parts = []
    for category, data in summary.items():
        parts.append(f"{category}{data['amount']}元({data['percentage']}%)")
    return "，".join(parts) if parts else "无消费记录"


def _call_kimi_analysis(summary_text: str, count: int, total: float) -> dict:
    """调用 Kimi（Moonshot）API 生成消费评语、人格标签与省钱建议。"""
    api_key = os.getenv("MOONSHOT_API_KEY")
    if not api_key:
        raise ValueError("未配置环境变量 MOONSHOT_API_KEY")

    prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        summary_text=summary_text,
        count=count,
        total=total,
    )
    payload = {
        "model": MOONSHOT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    response = requests.post(
        f"{MOONSHOT_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    parsed = _parse_json_object_from_content(content)

    comment = str(parsed.get("comment", "")).strip()
    label = str(parsed.get("label", "")).strip()
    tips = parsed.get("tips", [])
    if not isinstance(tips, list):
        tips = []
    tips = [str(t).strip() for t in tips if str(t).strip()][:2]

    if not comment or not label or len(tips) < 2:
        raise ValueError("Kimi 返回的分析结果字段不完整")

    return {"comment": comment, "label": label, "tips": tips}


def _fallback_analysis() -> dict:
    """Kimi 不可用时的默认分析结果。"""
    return dict(DEFAULT_ANALYSIS)


def _call_kimi_budget_advice(
    spent: float, budget: float, remaining: float, top_category: str
) -> list:
    """调用 Kimi（Moonshot）API 生成预算省钱建议。"""
    api_key = os.getenv("MOONSHOT_API_KEY")
    if not api_key:
        raise ValueError("未配置环境变量 MOONSHOT_API_KEY")

    prompt = BUDGET_ADVICE_PROMPT_TEMPLATE.format(
        budget=budget,
        spent=spent,
        remaining=remaining,
        top_category=top_category,
    )
    payload = {
        "model": BUDGET_ADVICE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    response = requests.post(
        f"{MOONSHOT_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=BUDGET_ADVICE_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    parsed = _parse_json_object_from_content(content)

    tips = parsed.get("tips", [])
    if not isinstance(tips, list):
        raise ValueError("Kimi 返回的 tips 不是数组")
    tips = [str(t).strip() for t in tips if str(t).strip()][:3]
    if len(tips) < 3:
        raise ValueError("Kimi 返回的省钱建议不足 3 条")
    return tips


def _fallback_budget_advice() -> list:
    """Kimi 不可用时的默认省钱建议。"""
    return list(DEFAULT_BUDGET_ADVICE_TIPS)


def _call_kimi_vision(image_b64: str, image_format: str) -> list:
    """调用 Kimi（Moonshot）API 识别账单图片，返回消费记录列表。"""
    api_key = os.getenv("MOONSHOT_API_KEY")
    if not api_key:
        raise ValueError("未配置环境变量 MOONSHOT_API_KEY")

    payload = {
        "model": MOONSHOT_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": RECOGNITION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/{image_format};base64,{image_b64}",
                        },
                    },
                ],
            }
        ],
    }
    response = requests.post(
        f"{MOONSHOT_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_records_from_content(content)


@app.route("/api/upload", methods=["POST"])
def upload_bill():
    """接收账单图片，保存后调用 Kimi API 识别消费记录，返回 JSON 数组。"""
    from dotenv import load_dotenv

    load_dotenv()

    try:
        if "file" not in request.files:
            return jsonify({"status": "error", "message": "未找到上传文件，字段名应为 file"}), 400

        file = request.files["file"]
        if not file or not file.filename:
            return jsonify({"status": "error", "message": "文件名为空"}), 400

        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "不支持的文件类型，仅允许 jpg、jpeg、png",
                    }
                ),
                400,
            )

        image_format = "jpeg" if ext in ("jpg", "jpeg") else "png"

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        save_path = UPLOAD_DIR / f"bill_{timestamp}.{ext}"
        file.save(save_path)

        try:
            img = Image.open(save_path)
            img.thumbnail((1024, 1024), Image.LANCZOS)
            compressed_path = UPLOAD_DIR / f"bill_{timestamp}_compressed.{ext}"
            img.save(compressed_path, quality=70, optimize=True)
            read_path = compressed_path
        except Exception:
            read_path = save_path

        with open(read_path, "rb") as image_file:
            image_b64 = base64.b64encode(image_file.read()).decode("utf-8")

        records = _call_kimi_vision(image_b64, image_format)
        return jsonify(records), 200

    except requests.Timeout:
        return jsonify({"status": "error", "message": "Kimi API 请求超时（30秒）"}), 504
    except requests.HTTPError as e:
        detail = ""
        if e.response is not None:
            try:
                detail = e.response.json().get("error", {}).get("message", "")
            except Exception:
                detail = e.response.text[:200]
        msg = f"Kimi API 请求失败: {detail or str(e)}"
        return jsonify({"status": "error", "message": msg}), 502
    except requests.RequestException as e:
        return jsonify({"status": "error", "message": f"Kimi API 请求失败: {e}"}), 502
    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        return jsonify({"status": "error", "message": f"解析识别结果失败: {e}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/analyze", methods=["POST"])
def analyze_bill():
    """接收消费记录 JSON 数组，返回分类统计、周趋势与 Kimi AI 分析。"""
    load_dotenv()

    try:
        records = request.get_json(silent=True)
        if records is None:
            return jsonify({"status": "error", "message": "请求体须为 JSON 格式"}), 400

        records = _normalize_records(records)
        if not records:
            return jsonify({"status": "error", "message": "消费记录不能为空"}), 400

        summary, total_amount, total_count = _compute_summary(records)
        trend = _compute_trend(records)
        summary_text = _format_summary_text(summary)

        try:
            ai_result = _call_kimi_analysis(summary_text, total_count, total_amount)
        except Exception:
            ai_result = _fallback_analysis()

        return jsonify(
            {
                "summary": summary,
                "trend": trend,
                "comment": ai_result["comment"],
                "label": ai_result["label"],
                "tips": ai_result["tips"],
            }
        ), 200

    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/budget/set", methods=["POST"])
def set_budget():
    """接收预算设置请求，校验后写入 SQLite。"""
    conn = None
    try:
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"status": "error", "message": "请求体须为 JSON 格式"}), 400

        user_id = str(data.get("user_id", "")).strip()
        if not user_id:
            return jsonify({"status": "error", "message": "user_id 不能为空"}), 400

        if "monthly_budget" not in data:
            return jsonify({"status": "error", "message": "monthly_budget 不能为空"}), 400
        try:
            monthly_budget = float(data.get("monthly_budget"))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "monthly_budget 必须为有效数字"}), 400
        if monthly_budget <= 0:
            return jsonify({"status": "error", "message": "monthly_budget 必须大于0"}), 400

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM budgets WHERE user_id = ?", (user_id,))
        exists = cursor.fetchone() is not None

        if exists:
            cursor.execute(
                """
                UPDATE budgets
                SET monthly_budget = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (monthly_budget, now, user_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO budgets (user_id, monthly_budget, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, monthly_budget, now, now),
            )
        conn.commit()

        return jsonify(
            {
                "status": "ok",
                "message": "预算设置成功",
                "budget": monthly_budget,
            }
        ), 200

    except sqlite3.Error as e:
        return jsonify({"status": "error", "message": f"数据库操作失败: {e}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/budget/status", methods=["GET"])
def get_budget_status():
    """查询用户预算及本月消费状态。"""
    conn = None
    try:
        user_id = str(request.args.get("user_id", "")).strip()
        if not user_id:
            return jsonify({"status": "error", "message": "user_id 不能为空"}), 400

        try:
            spent = float(request.args.get("total_spent", 0))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "total_spent 必须为有效数字"}), 400
        if spent < 0:
            return jsonify({"status": "error", "message": "total_spent 不能为负数"}), 400

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT monthly_budget FROM budgets WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()

        if row is None:
            return jsonify({"status": "error", "message": "请先设置预算"}), 404

        budget = float(row[0])
        remaining = round(budget - spent, 2)
        percentage = round(spent / budget * 100, 1) if budget else 0.0
        warning = percentage > 80

        return jsonify(
            {
                "status": "ok",
                "budget": budget,
                "spent": spent,
                "remaining": remaining,
                "percentage": percentage,
                "warning": warning,
            }
        ), 200

    except sqlite3.Error as e:
        return jsonify({"status": "error", "message": f"数据库操作失败: {e}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/budget/advice", methods=["POST"])
def budget_advice():
    """根据本月预算与消费情况，调用 Kimi 生成省钱建议；失败时返回默认建议。"""
    load_dotenv()

    try:
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"status": "error", "message": "请求体须为 JSON 格式"}), 400

        for field in ("spent", "budget", "remaining"):
            if field not in data:
                return jsonify({"status": "error", "message": f"{field} 不能为空"}), 400
            try:
                value = float(data[field])
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": f"{field} 必须为有效数字"}), 400
            if value < 0:
                return jsonify({"status": "error", "message": f"{field} 不能为负数"}), 400

        spent = float(data["spent"])
        budget = float(data["budget"])
        remaining = float(data["remaining"])

        top_category = str(data.get("top_category", "")).strip()
        if not top_category:
            return jsonify({"status": "error", "message": "top_category 不能为空"}), 400

        try:
            tips = _call_kimi_budget_advice(spent, budget, remaining, top_category)
            return jsonify({"status": "ok", "tips": tips, "source": "kimi"}), 200
        except Exception as e:
            tips = _fallback_budget_advice()
            return jsonify({
                "status": "ok",
                "tips": tips,
                "source": "default",
                "debug_error": str(e)
            }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
         
      #  try:
      #      tips = _call_kimi_budget_advice(spent, budget, remaining, top_category)
      #  except Exception:
      #      tips = _fallback_budget_advice()

      #  return jsonify({"status": "ok", "tips": tips}), 200

  #  except Exception as e:
      #  return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    print("服务器已启动，访问 http://127.0.0.1:5000")
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
