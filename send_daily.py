"""Frase diária de inglês no WhatsApp (imagem + texto + áudio).

Uso:
    python send_daily.py --dry-run     # só gera arquivos em ./out, não envia
    python send_daily.py               # gera e envia pelo WhatsApp Cloud API
    python send_daily.py --day 5       # força a frase de índice 5

Variáveis de ambiente (envio): WA_TOKEN, WA_PHONE_ID, WA_TO
Opcionais: START_DATE (AAAA-MM-DD, padrão 2026-11-05), WA_TEMPLATE (padrão hello_world),
           WA_TEMPLATE_LANG (padrão en_US)
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.parse
from datetime import date
from pathlib import Path

import requests

try:  # usa os certificados do sistema (útil com antivírus/proxy que interceptam HTTPS)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

BASE = Path(__file__).parent
OUT = BASE / "media" / date.today().isoformat()  # arquivos do dia (commitados no repo)
GRAPH = "https://graph.facebook.com/v21.0"
VOICE = "en-US-JennyNeural"


def pick_phrase(day_override=None):
    phrases = json.loads((BASE / "phrases.json").read_text(encoding="utf-8"))
    if day_override is not None:
        idx = day_override
    else:
        start = date.fromisoformat(os.getenv("START_DATE", "2026-11-05"))
        idx = (date.today() - start).days
    return idx, phrases[idx % len(phrases)]


async def make_audio(text, path_mp3, rate):
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate=rate).save(str(path_mp3))


def to_ogg_opus(mp3, ogg):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
         "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1", str(ogg)],
        check=True,
    )


def make_audio_files(text):
    """Gera um único áudio: velocidade normal, pausa, depois devagar."""
    OUT.mkdir(parents=True, exist_ok=True)
    normal, slow = OUT / "normal.mp3", OUT / "slow.mp3"
    asyncio.run(make_audio(text, normal, "+0%"))
    asyncio.run(make_audio(text, slow, "-40%"))
    # concatena normal + silêncio + lento
    merged = OUT / "audio.ogg"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(normal), "-f", "lavfi",
         "-t", "1", "-i", "anullsrc=r=24000:cl=mono", "-i", str(slow),
         "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[a]", "-map", "[a]",
         "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1", str(merged)],
        check=True,
    )
    normal.unlink()
    slow.unlink()
    return merged


def make_image(prompt):
    full = f"simple friendly flat illustration, bright colors, no text, {prompt}"
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(full)
           + "?width=768&height=768&nologo=true")
    try:
        r = requests.get(url, timeout=90)
        r.raise_for_status()
        path = OUT / "image.jpg"
        path.write_bytes(r.content)
        return path
    except Exception as e:  # imagem é opcional; não derruba o envio
        print("Aviso: não gerou imagem:", e)
        return None


def caption(idx, p):
    return (
        f"📚 *Frase do dia #{idx + 1}*\n\n"
        f"🇺🇸 *{p['en']}*\n"
        f"🇧🇷 {p['pt']}\n\n"
        f"🗣️ *Pronúncia:* {p['pron']}\n"
        f"🔤 *IPA:* {p['ipa']}\n"
        f"💡 {p['tip']}\n\n"
        f"🎧 Ouça o áudio (normal + devagar) e repita em voz alta 3 vezes!\n"
        f"Responda \"ok\" para manter as mensagens chegando. 👍"
    )


# ---------- WhatsApp Cloud API ----------
def _hdr():
    return {"Authorization": f"Bearer {os.environ['WA_TOKEN']}"}


def upload_media(path, mime):
    pid = os.environ["WA_PHONE_ID"]
    with open(path, "rb") as f:
        r = requests.post(
            f"{GRAPH}/{pid}/media", headers=_hdr(),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (path.name, f, mime)}, timeout=60,
        )
    r.raise_for_status()
    return r.json()["id"]


def send(payload):
    pid = os.environ["WA_PHONE_ID"]
    payload = {"messaging_product": "whatsapp", "to": os.environ["WA_TO"], **payload}
    r = requests.post(f"{GRAPH}/{pid}/messages", headers=_hdr(), json=payload, timeout=60)
    return r


def send_all(text, image, audio):
    # 1) texto/imagem; se a janela de 24h estiver fechada, manda template e para
    if image:
        mid = upload_media(image, "image/jpeg")
        r = send({"type": "image", "image": {"id": mid, "caption": text}})
    else:
        r = send({"type": "text", "text": {"body": text}})
    if r.status_code != 200:
        err = r.json().get("error", {})
        if err.get("code") == 131047:  # fora da janela de 24h
            print("Janela de 24h fechada: enviando template para reabrir.")
            tr = send({"type": "template", "template": {
                "name": os.getenv("WA_TEMPLATE", "hello_world"),
                "language": {"code": os.getenv("WA_TEMPLATE_LANG", "en_US")}}})
            print("Template:", tr.status_code, tr.text)
            print("Responda qualquer coisa no WhatsApp para reabrir a janela.")
            return
        raise RuntimeError(f"Falha no envio: {r.text}")
    # 2) áudio como mensagem de voz
    aid = upload_media(audio, "audio/ogg")
    r = send({"type": "audio", "audio": {"id": aid}})
    r.raise_for_status()
    print("Enviado!")


# ---------- Twilio (WhatsApp Sandbox) ----------
def host_file(path, mime):
    """URL pública do arquivo. Com MEDIA_BASE_URL (jsDelivr apontando para o repo público),
    o arquivo já foi commitado e basta montar o link."""
    base = os.getenv("MEDIA_BASE_URL")
    if base:
        return f"{base.rstrip('/')}/{path.parent.name}/{path.name}"
    raise RuntimeError("Defina MEDIA_BASE_URL (link jsDelivr da pasta media do repositório).")


def _unused_litterbox(path, mime):
    with open(path, "rb") as f:
        r = requests.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "12h"},
            files={"fileToUpload": (path.name, f, mime)}, timeout=120,
        )
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"Upload falhou: {url}")
    return url


def twilio_send(body=None, media_url=None):
    sid, tok = os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"]
    data = {
        "From": os.getenv("TWILIO_FROM", "whatsapp:+14155238886"),
        "To": "whatsapp:+" + os.environ["WA_TO"].lstrip("+"),
    }
    if body:
        data["Body"] = body
    if media_url:
        data["MediaUrl"] = media_url
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=data, auth=(sid, tok), timeout=60,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio recusou: {r.text}")
    return r.json()["sid"]


def twilio_status(msg_sid):
    sid, tok = os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"]
    r = requests.get(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages/{msg_sid}.json",
        auth=(sid, tok), timeout=30,
    )
    j = r.json()
    return j.get("status"), j.get("error_code"), j.get("error_message")


def send_twilio(text, image, audio):
    import time
    sids = []
    if image:
        sids.append(twilio_send(text, host_file(image, "image/jpeg")))
    else:
        sids.append(twilio_send(text))
    sids.append(twilio_send(media_url=host_file(audio, "audio/ogg")))
    time.sleep(10)
    for s in sids:
        status, code, msg = twilio_status(s)
        print(f"  {s}: {status}" + (f" (erro {code}: {msg})" if code else ""))
        if code == 63016:
            print("  -> Janela de 24h fechada. Mande uma mensagem (ex.: 'ok') ao número do sandbox e rode de novo.")
        if code == 63015:
            print("  -> Seu número não está no sandbox. Envie 'join <palavra>' ao número do sandbox.")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--day", type=int)
    ap.add_argument("--prepare", action="store_true",
                    help="só gera os arquivos do dia em media/ (não envia)")
    ap.add_argument("--send-only", action="store_true",
                    help="envia usando os arquivos já gerados em media/")
    ap.add_argument("--template", action="store_true",
                    help="envia só o template (reabre a janela de 24h)")
    a = ap.parse_args()

    if a.template:
        tr = send({"type": "template", "template": {
            "name": os.getenv("WA_TEMPLATE", "hello_world"),
            "language": {"code": os.getenv("WA_TEMPLATE_LANG", "en_US")}}})
        print("Template:", tr.status_code, tr.text)
        return

    idx, p = pick_phrase(a.day)
    print(f"Frase #{idx + 1}: {p['en']}")
    OUT.mkdir(parents=True, exist_ok=True)
    if a.send_only:
        audio = OUT / "audio.ogg"
        image = OUT / "image.jpg" if (OUT / "image.jpg").exists() else None
        text = (OUT / "caption.txt").read_text(encoding="utf-8")
    else:
        audio = make_audio_files(p["en"])
        image = make_image(p["image"])
        text = caption(idx, p)
        (OUT / "caption.txt").write_text(text, encoding="utf-8")
    if a.dry_run or a.prepare:
        print(text)
        print("Arquivos em:", OUT)
        return
    if os.getenv("TWILIO_SID"):
        send_twilio(text, image, audio)
    else:
        send_all(text, image, audio)


if __name__ == "__main__":
    main()
