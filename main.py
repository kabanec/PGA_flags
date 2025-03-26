import os
import pandas as pd
import requests
import logging
import openai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from urllib.parse import urlparse

load_dotenv()
app = FastAPI()
# Set base directory to the directory where main.py is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
BARCODE_API_KEY = os.getenv("BARCODE_API_KEY")
logger = logging.getLogger("uvicorn.error")

class LookupRequest(BaseModel):
    hs_code: str
    name: str | None = None
    description: str | None = None

class UPCRequest(BaseModel):
    upc: str

def is_valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


@app.get("/test-chatgpt")
async def test_chatgpt():
    try:
        completion = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":"Say hello"}]
        )
        return {"chatgpt_response": completion.choices[0].message.content}
    except Exception as e:
        return {"error": str(e)}

@app.get("/list-data")
def list_data():
    try:
        files = os.listdir(f"{DATA_DIR}")
        return {"files": files}
    except Exception as e:
        return {"error": str(e)}

@app.get("/", response_class=HTMLResponse)
async def home():
    with open("templates/index.html", "r") as f:
        return f.read()

@app.post("/lookup-upc")
async def lookup_upc(req: UPCRequest):
    if not BARCODE_API_KEY:
        logger.error("BARCODE_API_KEY not set")
        raise HTTPException(status_code=500, detail="Barcode API key not set")

    url = f"https://api.barcodelookup.com/v3/products?key={BARCODE_API_KEY}&barcode={req.upc}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Error calling BarcodeLookup API", exc_info=True)
        raise HTTPException(status_code=502, detail=f"External API error: {str(e)}")

    data = resp.json().get("products")
    if not data:
        return {"error": "Product not found"}

    product = data[0]
    return {
        "name": product.get("product_name",""),
        "brand": product.get("brand",""),
        "description": product.get("description",""),
        "image": product.get("images",[""])[0]
    }

@app.post("/lookup")
async def lookup(req: LookupRequest):
    target = req.hs_code

    # HS Chapters
    df_chapters = pd.read_excel(f"{DATA_DIR}/HS_Chapters_lookup.xlsx")
    df_chapters["Chapter"] = df_chapters["Chapter"].astype(str).str.zfill(2)
    df_chapters = df_chapters.ffill().bfill()
    chapter_key = target[:2].zfill(2)
    chapters = df_chapters[df_chapters["Chapter"] == chapter_key] \
        .replace("", pd.NA).dropna(axis=1, how="all") \
        .to_dict(orient="records")

    # PGA_HTS + PGA_Codes
    df_hts = pd.read_excel(f"{DATA_DIR}/PGA_HTS.xlsx", dtype=str) \
        .rename(columns={"HTS Number - Full": "HsCode"})
    df_pga = pd.read_excel(f"{DATA_DIR}/PGA_codes.xlsx")
    pga_hts = (
        df_hts.merge(df_pga, how="left",
                     left_on=["PGA Name Code","PGA Flag Code","PGA Program Code"],
                     right_on=["Agency Code","Code","Program Code"])
        .replace("", pd.NA).dropna(axis=1, how="all")
    )
    pga_hts = pga_hts[pga_hts["HsCode"] == target].to_dict(orient="records")

    # HS Rules
    df_rules = pd.read_excel(f"{DATA_DIR}/hs_codes.xlsx")
    df_rules["HsCode"] = df_rules["HsCode"].astype(str)
    df_rules["Chapter"] = df_rules["HsCode"].str[:2].str.zfill(2)
    df_rules["Header"] = df_rules["HsCode"].str[:4]
    hs_rules = df_rules[df_rules["HsCode"].str.startswith(target)]
    if hs_rules.empty:
        hs_rules = df_rules[df_rules["HsCode"].str.startswith(target[:4])]
    if hs_rules.empty:
        hs_rules = df_rules[df_rules["Chapter"] == chapter_key]
    hs_rules = hs_rules.replace("", pd.NA).dropna(axis=1, how="all") \
        .to_dict(orient="records")

    # Build a unique set of only valid HTTP(S) links
    links = set()
    for rec in pga_hts:
        for col in ("TextLink", "Website Link", "CFR"):
            raw = rec.get(col) or ""
            for url in raw.split():
                if is_valid_url(url):
                    links.add(url)

    requirements = []
    for url in sorted(links):
        try:
            page_text = requests.get(url, timeout=10).text
            prompt = (
                f"Product Name: {req.name or 'N/A'}\n"
                f"Product Description: {req.description or 'N/A'}\n\n"
                "From this regulatory page, list each required document and its conditions.\n\n"
                f"Page content:\n{page_text}"
            )
            resp = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": "You are a customs compliance expert understanding how the participate government agencies work. Able to identify and describe the compliance needs for a given product."},
                          {"role": "user", "content": prompt}]
            )
            raw = resp.choices[0].message.content
            requirements.append({
                "url": url,
                "raw_response": raw,
                "parsed_requirements": raw  # use identical for now
            })
        except Exception as e:
            requirements.append({"url": url, "error": str(e)})

    return {
        "hs_chapters": chapters,
        "pga_hts": pga_hts,
        "hs_rules": hs_rules,
        "pga_requirements": requirements
    }
