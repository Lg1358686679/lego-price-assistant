import os
import re
import json
import requests
import pandas as pd
from datetime import datetime
import streamlit as st
import difflib
from supabase import create_client

# ========== 配置（请替换为你的实际值）==========
SUPABASE_URL = "https://rgykpnyacrazzhltohqa.supabase.co"
SUPABASE_KEY = "sb_publishable__beNMxEDkvnOkuLBqmrqug_A2YSmhgU"
ZHIPU_API_KEY = "50e009bbd1a7452ba1ceff7d189d3ea4.A7n3m7SbhVCE6nxn"
# =============================================

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

if "parsed_data" not in st.session_state:
    st.session_state.parsed_data = None
if "raw_input" not in st.session_state:
    st.session_state.raw_input = ""

# ---------- 数据库操作 ----------
def get_trend_data():
    response = supabase.table("price_records").select("*").execute()
    data = response.data
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df['时间'] = pd.to_datetime(df['time'])
    df['型号'] = df['model'].astype(str).str.replace(r'\.0$', '', regex=True)
    df['价格'] = pd.to_numeric(df['price'], errors='coerce')
    df = df.dropna(subset=['型号', '价格'])
    df = df[df['型号'].str.match(r'^[1-9][0-9]{4}$')]
    return df

def save_to_supabase(data, raw_text):
    records = []
    for i, item in enumerate(data):
        records.append({
            "time": datetime.now().isoformat(),
            "model": item.get('model'),
            "price": item.get('price'),
            "raw_text": raw_text if i == 0 else None
        })
    supabase.table("price_records").insert(records).execute()
    return len(data)

def load_corrections():
    response = supabase.table("corrections").select("*").execute()
    data = response.data
    return [{"original": d["original_text"], "corrected": d["corrected_data"]} for d in data]

def save_correction(original_text, corrected_data):
    supabase.table("corrections").insert({
        "original_text": original_text,
        "corrected_data": corrected_data
    }).execute()

def find_similar_cases(text, threshold=0.8):
    corrections = load_corrections()
    similar = []
    for case in corrections:
        ratio = difflib.SequenceMatcher(None, text, case["original"]).ratio()
        if ratio >= threshold:
            similar.append(case)
    return similar

# ---------- 正则提取（与之前相同）----------
def extract_with_regex(line):
    line = line.strip()
    if not line:
        return None, None
    # 表格行处理
    if '\t' in line:
        parts = line.split('\t')
        models = []
        prices = []
        for part in parts:
            model_matches = re.findall(r'(?<![0-9])([1-9][0-9]{4})(?![0-9])', part)
            models.extend(model_matches)
            other_numbers = re.findall(r'\b(\d+)\b', part)
            for num in other_numbers:
                if len(num) != 5 or num[0] == '0':
                    prices.append(int(num))
        if models:
            model = models[0]
            price = prices[0] if prices else None
            if price is not None and price >= 10:
                return model, price
        return None, None
    # 普通行
    model_matches = re.findall(r'(?<![0-9])([1-9][0-9]{4})(?![0-9])', line)
    if not model_matches:
        return None, None
    model = model_matches[0]
    all_numbers = re.findall(r'\b(\d+)\b', line)
    price_candidates = []
    for num in all_numbers:
        if len(num) != 5 or num[0] == '0':
            price_candidates.append(int(num))
    # 优先行首数字
    price_match = re.search(r'^(\d+)', line)
    if price_match:
        num = price_match.group(1)
        if len(num) != 5 or num[0] == '0':
            price = int(num)
            if price >= 10:
                return model, price
    # “收”字前数字
    price_match = re.search(r'(\d+)\s*收', line)
    if price_match:
        num = price_match.group(1)
        if len(num) != 5 or num[0] == '0':
            price = int(num)
            if price >= 10:
                return model, price
    # 其他数字
    if price_candidates:
        price = price_candidates[0]
        if price >= 10:
            return model, price
    return None, None

def preprocess_text(text):
    lines = text.strip().split('\n')
    filtered = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.search(r'\d', line) or "收" in line:
            cleaned = re.sub(r'(普快|顺丰|好盒|压盒|包邮|顺丰发出|跨越最好|留底|加固|揽收|打包|山东|内蒙|广东|江苏|安徽|浙江|辽宁|吉林|黑龙江|河北|天津|山西|陕西|广西|云南|四川|贵州|福建|重庆|海南|北京|上海|天津|宁夏|青海|甘肃|新疆|西藏|内蒙古)', '', line)
            if not re.search(r'\d', cleaned):
                continue
            filtered.append(cleaned)
        elif len(line) > 20:
            continue
    return "\n".join(filtered)

def parse_with_llm(text):
    # 先尝试正则提取
    lines = text.strip().split('\n')
    regex_results = []
    remaining_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not re.search(r'(?<![0-9])[1-9][0-9]{4}(?![0-9])', line):
            continue
        model, price = extract_with_regex(line)
        if model is not None and price is not None:
            regex_results.append({"model": model, "price": price})
        else:
            remaining_lines.append(line)
    # 如果还有剩余行，用智谱 API 处理
    if not remaining_lines:
        return regex_results

    remaining_text = "\n".join(remaining_lines)
    similar_cases = find_similar_cases(remaining_text, threshold=0.5)
    few_shot_examples = ""
    if similar_cases:
        few_shot_examples = "\n参考以下类似情况的正确解析结果：\n"
        for case in similar_cases[:3]:
            few_shot_examples += f"输入：{case['original']}\n输出：{json.dumps(case['corrected'], ensure_ascii=False)}\n"
    else:
        few_shot_examples = "\n请直接解析。\n"

    prompt = f"""你是一个乐高报价解析助手。从以下文本中提取出每个乐高产品的官方型号编号和价格。

输入文本每行一个产品，行与行之间用换行符分隔。你需要识别所有行，并输出一个JSON数组，每个元素对应一行。

要求：
- 型号必须是5位数字，例如 10307、42115、21350。请只提取这些5位数字。
- 价格是数字，可能带“元”、“收”、“块”等字，只需数字。
- 忽略物流信息、数量、收件地址等。
- 直接输出一个JSON数组，格式：[{{"model": "型号", "price": 价格}}]
- 只输出JSON数组，不要输出任何其他文字。

示例：
输入："1180收 乐高 10320 1"
输出：[{{"model": "10320", "price": 1180}}]

输入："880压盒10358声波普快云南"
输出：[{{"model": "10358", "price": 880}}]

{few_shot_examples}

现在请解析：
{remaining_text}

输出："""

    headers = {
        "Authorization": f"Bearer {ZHIPU_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 8192
    }
    try:
        response = requests.post(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            headers=headers,
            json=payload,
            timeout=120
        )
        if response.status_code == 200:
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                try:
                    model_results = json.loads(json_match.group())
                    return regex_results + model_results
                except:
                    return regex_results
            else:
                return regex_results
        else:
            st.error(f"智谱API调用失败：{response.status_code}")
            return regex_results
    except Exception as e:
        st.error(f"智谱API调用出错：{e}")
        return regex_results

def get_trend_alerts():
    df = get_trend_data()
    if df.empty or len(df) < 3:
        return []
    alerts = []
    grouped = df.sort_values('时间').groupby('型号')
    for model, group in grouped:
        if len(group) < 3:
            continue
        recent = group.tail(3)
        prices = recent['价格'].tolist()
        if prices[0] < prices[1] < prices[2]:
            alerts.append((model, "上涨"))
        elif prices[0] > prices[1] > prices[2]:
            alerts.append((model, "下跌"))
    return alerts

def show_trend_chart(model):
    df = get_trend_data()
    if df.empty:
        st.info("暂无数据")
        return
    df_model = df[df['型号'] == model].sort_values('时间')
    if df_model.empty:
        st.info(f"型号 {model} 暂无数据")
        return
    st.line_chart(df_model.set_index('时间')['价格'])

def get_model_list():
    df = get_trend_data()
    if df.empty:
        return []
    return sorted(df['型号'].unique())

# ---------- Streamlit UI ----------
st.set_page_config(page_title="乐高报价助手", layout="wide")
st.title("🧩 乐高报价助手")

alerts = get_trend_alerts()
if alerts:
    with st.container():
        st.markdown("### ⚠️ 价格趋势预警")
        for model, trend in alerts:
            if trend == "上涨":
                st.warning(f"📈 型号 {model} 连续三次价格上涨，请注意！")
            else:
                st.error(f"📉 型号 {model} 连续三次价格下跌，请注意！")
        st.markdown("---")

st.markdown("### 输入报价信息")
user_input = st.text_area("把报价文字粘贴或输入到这里，支持多行（最多几百行）", height=300)

col1, col2 = st.columns(2)
with col1:
    if st.button("🔍 解析并记录"):
        if not user_input.strip():
            st.warning("请输入报价内容")
        else:
            with st.spinner("正在解析（正则+AI）..."):
                try:
                    parsed = parse_with_llm(user_input)
                    if parsed:
                        count = save_to_supabase(parsed, user_input)
                        st.success(f"✅ 成功记录 {count} 条报价")
                    else:
                        st.warning("⚠️ 解析未识别到有效数据，请手动输入或纠错。")
                        parsed = []
                    st.session_state.parsed_data = parsed
                    st.session_state.raw_input = user_input
                    st.rerun()
                except Exception as e:
                    st.error(f"运行出错：{str(e)}")
                    st.exception(e)

with col2:
    if st.button("📊 查看所有型号"):
        models = get_model_list()
        if not models:
            st.info("暂无数据")
        else:
            st.write("已记录的型号：", ", ".join(models))

if st.session_state.parsed_data is not None:
    st.markdown("---")
    st.subheader("📝 解析结果（可编辑）")
    st.caption("💡 提示：双击单元格可编辑数据，点击表格右下角“＋”可添加新行。修改后请点击下方“提交纠错”保存。")
    if not st.session_state.parsed_data:
        df_edit = pd.DataFrame(columns=['model', 'price'])
    else:
        df_edit = pd.DataFrame(st.session_state.parsed_data)
        if 'price' in df_edit.columns:
            df_edit['price'] = pd.to_numeric(df_edit['price'], errors='coerce').fillna(0).astype(int)
    
    column_config = {
        "model": st.column_config.TextColumn("型号", required=True),
        "price": st.column_config.NumberColumn("价格", required=True, step=1)
    }
    edited_df = st.data_editor(df_edit, num_rows="dynamic", column_config=column_config, key="edit_table")
    
    if st.button("💾 提交纠错"):
        corrected_data = edited_df.to_dict(orient='records')
        corrected_data = [row for row in corrected_data if row.get('model') and row.get('price')]
        if corrected_data:
            for row in corrected_data:
                row['price'] = int(row['price'])
            save_correction(st.session_state.raw_input, corrected_data)
            save_to_supabase(corrected_data, st.session_state.raw_input)
            st.success("已学习并保存！下次遇到类似输入将自动参考此纠正。")
            st.session_state.parsed_data = None
            st.session_state.raw_input = ""
            st.rerun()
        else:
            st.warning("未填写有效数据，未保存。")

st.markdown("---")
st.subheader("📈 价格趋势查询")

model_input = st.text_input("直接输入型号编号（如10320）")
if model_input:
    model_input = model_input.strip()
    show_trend_chart(model_input)

models = get_model_list()
if models:
    st.markdown("或从已记录型号中选择：")
    selected_model = st.selectbox("选择型号", options=models, key="model_select")
    if selected_model:
        show_trend_chart(selected_model)
else:
    st.info("暂无已记录型号，请先解析报价。")

st.markdown("---")
st.caption("数据云端存储，多用户共享。纠错案例自动学习。")