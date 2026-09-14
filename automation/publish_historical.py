#!/usr/bin/env python3
"""Publish the reviewed five-edition historical replay as a single guarded batch."""
import argparse, hashlib, json, shutil, subprocess, tempfile
from pathlib import Path
from image_generation import generate_images

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT
ENV = Path("/home/ubuntu/ai-hayom-automation/.env")
IDS = ["-005", "-004", "-003", "-002", "-001"]
URLS = {
 "navier":"https://openai.com/index/navier-stokes-solution/",
 "alphagenome":"https://deepmind.google/blog/alphagenome-atlas-a-predictive-map-of-every-possible-dna-letter-change-in-the-human-genome/",
 "anthropic":"https://www.anthropic.com/threat-intelligence-report-september-2026",
 "zai":"https://www.marketscreener.com/news/china-ai-developer-z-ai-launches-5-billion-hong-kong-share-convertible-bond-sales-term-sheet-show-ce785bdfdd8bff26",
}

def story(section, headline, quick, summary, why, url):
 return {"section":section,"headline":headline,"readingTime":"01:00","quickRead":quick,"summary":summary,"whyItMatters":why,"sources":[{"url":url}]}

def correct(e):
 n=e["number"]
 if n == "-005":
  e["stories"][-1]=story("מדע ומתמטיקה","AI מציעה פתרון לבעיה מתמטית בת 90 שנה","OpenAI פרסמה פתרון שנוצר במערכת פנימית לבעיית נבייה–סטוקס; בדיקה חיצונית עדיין נדרשת.","OpenAI פרסמה כתיבה ופורמליזציה ב-Lean של פתרון לבעיית נבייה–סטוקס, אחת מבעיות פרס המילניום. החברה מתארת תוצאה שנוצרה במערכת פנימית, ולא טוענת לקבלת פרס.","אם יאומת, זה יהיה מבחן משמעותי ליכולת של מערכות AI לתרום למחקר מתמטי; נכון לעכשיו מדובר בטענה של OpenAI שדורשת בחינת מומחים.",URLS["navier"])
  e["stories"].insert(0,story("מדע ורפואה","DeepMind ממפה שינויים אפשריים בגנום","AlphaGenome מציעה דרך לחזות את השפעתן של וריאציות DNA רבות.","Google DeepMind הציגה את AlphaGenome Atlas, מפה חיזויית של השפעת שינויים אפשריים באותיות ה-DNA. מדובר בכלי מחקרי ולא באבחון רפואי.","היכולת להעריך וריאציות רבות עשויה לסייע למחקר גנטי, אך התוצאות דורשות אימות ניסויי.",URLS["alphagenome"])); e["stories"]=e["stories"][:5]
 if n == "-003":
  e["stories"][-1]=story("איומי AI","Anthropic מפרסמת דוח על שימוש זדוני ב-Claude","החברה מתארת קמפיינים של סייבר, מעקב, נשק ומחקר ביולוגי שנחסמו.","Anthropic פרסמה דוח מודיעין איומים המתאר ניסיונות להשתמש ב-Claude לפעילות זדונית, ובהם סייבר, מעקב, פיתוח נשק ומחקר ביולוגי. זהו דיווח של החברה על חקירותיה, ולא אימות עצמאי לכל מקרה.","הסיפור מדגים שהשימוש לרעה ב-AI עובר מתרחישים תאורטיים לדיווחים על פעילות ממשית, אך מחייב להבחין בין טענות הספק לבין ראיות חיצוניות.",URLS["anthropic"])
 if n == "-002":
  e["stories"][-1]=story("כסף ותחרות","Z.AI מגייסת כחמישה מיליארד דולר","החברה מציעה מניות ומכשירי חוב בהיקף של כ-5 מיליארד דולר.","לפי Reuters, Z.AI השיקה מכירת מניות בהיקף של כ-2 מיליארד דולר והנפקת אג״ח להמרה בכ-3 מיליארד דולר. הפרטים הסופיים עשויים להשתנות.","הגיוס ממחיש את עוצמת ההון הנדרשת כדי להתחרות בתשתיות ובמודלים בסין.",URLS["zai"])
 e["totalReadingTime"]="05:00"; e["status"]="historical-test"; return e

def main():
 ap=argparse.ArgumentParser(); ap.add_argument("batch",type=Path); ap.add_argument("--push",action="store_true"); a=ap.parse_args()
 if not a.push: raise SystemExit("--push is required for the explicitly authorized production publish")
 if subprocess.run(["git","status","--porcelain"],cwd=REPO,text=True,capture_output=True).stdout.strip(): raise SystemExit("repository is not clean")
 editions=[]
 for n in IDS:
  e=correct(json.loads((a.batch/n/"edition.json").read_text()))
  proposal=json.loads((a.batch/n/"proposal.json").read_text())
  out=ROOT/"automation"/"output"/"historical-published"/n.replace("-","m")
  p=proposal.get("cartoonConcepts") or [e.get("headline", "editorial AI news metaphor")]
  concept=p[0] if isinstance(p,list) else str(p)
  mode={"id":"symbolic-clarity","traits":"high-contrast black editorial ink, warm off-white newsprint, bold motion lines, dense crosshatching, strong graphic silhouettes, restrained red accent, energetic newspaper illustration"}
  result=generate_images(ENV,out,concept,mode,fixture=False,reuse_existing=True)
  if len(result["images"]) != 2: raise SystemExit("image batch did not produce exactly two assets")
  editions.append((n,e,result["images"]))
 for n,e,imgs in editions:
  d=REPO/"edition"/n; d.mkdir(exist_ok=True)
  for role in ("desktop","mobile"):
   src=Path(next(x["path"] for x in imgs if x["role"]==role)); shutil.copy2(src,d/f"cartoon-{role}.webp")
  e["cartoon"]={"desktop":f"/edition/{n}/cartoon-desktop.webp","mobile":f"/edition/{n}/cartoon-mobile.webp","alt":"מטפורה חזותית מקורית לחדשות AI של היום"}
  (d/"edition.json").write_text(json.dumps(e,ensure_ascii=False,indent=2)+"\n")
 c=json.loads((REPO/"edition/catalog.json").read_text()); c["editions"]=[x for x in IDS if x not in c.get("editions",[])]+[x for x in c.get("editions",[]) if x not in IDS]; c["latest"]="002"; (REPO/"edition/catalog.json").write_text(json.dumps(c,ensure_ascii=False,indent=2)+"\n")
 subprocess.run(["git","add","edition/catalog.json"]+[f"edition/{n}" for n in IDS],cwd=REPO,check=True)
 subprocess.run(["git","commit","-m","Publish historical AI Hayom test editions"],cwd=REPO,check=True)
 subprocess.run(["git","push","origin","main"],cwd=REPO,check=True)
 print("published", ",".join(IDS), "commit", subprocess.check_output(["git","rev-parse","--short","HEAD"],cwd=REPO,text=True).strip())
if __name__=="__main__": main()
