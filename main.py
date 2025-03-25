import os
import pandas as pd
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
DATA_DIR = "data"

@app.get("/list-data")
def list_data():
    try:
        files = os.listdir("data")
        return {"files": files}
    except Exception as e:
        return {"error": str(e)}

from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
async def home():
    with open("templates/index.html", "r") as f:
        return f.read()

class LookupRequest(BaseModel):
    hs_code: str

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
    df_pga = pd.read_excel(f"{DATA_DIR}/PGA_Codes.xlsx")
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

    return {
        "hs_chapters": chapters,
        "pga_hts": pga_hts,
        "hs_rules": hs_rules
    }
