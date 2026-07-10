#!/usr/bin/env python3
"""
SMS / text-message front door — the same navigator, reachable from any phone with no app,
no data plan, no internet. The audience Cobijo serves is disproportionately reachable by text,
not by web, so this is a first-class channel, not an afterthought.

A patient texts in; a short state machine walks them through the same intake as the web app
(hospital -> income -> household -> insurance -> collections) and texts back a concise plan built
by the REAL navigator (navigator.build_plan_struct), in English or Spanish.

Three ways to run it — the logic (`Conversation`) is identical in all three:
  python3 sms.py                 # interactive simulator in your terminal (no Twilio, no network)
  python3 sms.py --demo          # scripted demo conversation (for the pitch / video)
  python3 sms.py --serve         # Twilio webhook server (POST /sms, replies with TwiML)

Wiring Twilio later is just: point a Twilio number's "A message comes in" webhook at
POST https://<host>/sms. Inbound + TwiML reply need no Twilio SDK or credentials; only outbound-
initiated texts would. Stdlib only.
"""
import argparse
import base64
import hashlib
import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

MAX_BODY = 8192          # SMS bodies are tiny; cap to bound memory on a crafted Content-Length

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import navigator

YES = {"y", "yes", "yeah", "yep", "si", "sí", "s", "1", "true"}
NO = {"n", "no", "nope", "0", "false"}

# SMS-specific prompts (short, plain text). Kept here, not in messages.py, but held to the same
# en/es parity bar by tests/test_cobijo.py. The delivered PLAN reuses the navigator's catalog.
SMS = {
    "en": {
        "welcome": ("Cobijo Health — free help with a medical bill. Reply 1 for English, "
                    "2 para Español."),
        "ask_hospital": "Which hospital sent the bill? (Text the hospital name.)",
        "hospital_not_found": ("I couldn't find that hospital. Try the main name, e.g. "
                               "\"Adventist Health\"."),
        "ask_income": "Your household's yearly income? (Just the number, e.g. 30000.)",
        "ask_number": "Please text just a number, e.g. 30000.",
        "ask_household": "How many people are in your household? (A number.)",
        "ask_insurance": "Do you have health insurance? Reply YES or NO.",
        "ask_collections": "Is this bill already in collections? Reply YES or NO.",
        "plan_free": "GOOD NEWS: you likely qualify for FREE care at {name}.",
        "plan_discount": "You likely qualify for a DISCOUNT at {name}.",
        "plan_high_cost": "You may qualify for help at {name} due to high medical costs.",
        "plan_over": "You may be over {name}'s income limit — but you still have options.",
        "plan_apply": "Apply for charity care{phone} — you can even do it after the bill went to collections.",
        "plan_benefit": "Coverage: {benefit}",
        "plan_debt": "In collections? You have rights, and charity care can wipe it retroactively — ask first.",
        "plan_letter": "Reply LETTER and we'll text you a ready-to-send request letter.",
        "plan_footer": "Free info, not legal advice. Reply START to begin again.",
        "letter_sent": "Here is your request letter — copy, add your info in [brackets], and send it to the hospital:\n\n{letter}",
        "restart": "Okay, let's start over. Reply 1 for English, 2 para Español.",
    },
    "es": {
        "welcome": ("Cobijo Health — ayuda gratis con una factura médica. Responda 1 for English, "
                    "2 para Español."),
        "ask_hospital": "¿Qué hospital le envió la factura? (Escriba el nombre del hospital.)",
        "hospital_not_found": ("No encontré ese hospital. Intente el nombre principal, p. ej. "
                               "\"Adventist Health\"."),
        "ask_income": "¿El ingreso anual de su hogar? (Solo el número, p. ej. 30000.)",
        "ask_number": "Por favor escriba solo un número, p. ej. 30000.",
        "ask_household": "¿Cuántas personas hay en su hogar? (Un número.)",
        "ask_insurance": "¿Tiene seguro médico? Responda SÍ o NO.",
        "ask_collections": "¿Esta factura ya está en cobranza? Responda SÍ o NO.",
        "plan_free": "BUENAS NOTICIAS: probablemente califica para atención GRATUITA en {name}.",
        "plan_discount": "Probablemente califica para un DESCUENTO en {name}.",
        "plan_high_cost": "Podría calificar para ayuda en {name} por sus altos costos médicos.",
        "plan_over": "Quizás supere el límite de ingresos de {name}, pero aún tiene opciones.",
        "plan_apply": "Solicite atención caritativa{phone} — puede hacerlo incluso si ya pasó a cobranza.",
        "plan_benefit": "Cobertura: {benefit}",
        "plan_debt": "¿En cobranza? Tiene derechos, y la atención caritativa puede eliminarla de forma retroactiva — pregunte primero.",
        "plan_letter": "Responda CARTA y le enviaremos una carta de solicitud lista para enviar.",
        "plan_footer": "Información gratis, no asesoría legal. Responda INICIO para empezar de nuevo.",
        "letter_sent": "Aquí está su carta de solicitud — cópiela, agregue sus datos en [corchetes] y envíela al hospital:\n\n{letter}",
        "restart": "Bien, empecemos de nuevo. Responda 1 for English, 2 para Español.",
    },
}


def _strip_md(s):
    return s.replace("**", "")


def sms_t(lang, key, **kw):
    table = SMS.get(lang) or SMS["en"]
    return table.get(key, SMS["en"][key]).format(**kw)


class Conversation:
    """One patient's text session. Feed it inbound messages; it returns the reply to send back."""
    ORDER = ["income", "household", "insurance", "collections"]

    def __init__(self, ds):
        self.ds = ds
        self.lang = None
        self.step = "welcome"
        self.data = {}
        self.row = None
        self._last_plan = None       # (intake, row) so we can render the letter on demand

    def reply(self, text):
        msg = (text or "").strip()
        low = msg.lower()

        if low in ("start", "restart", "reset", "inicio"):
            self.__init__(self.ds)
            self.step = "lang"                # the restart message already shows the language prompt
            return sms_t("en", "restart")

        if self.step == "welcome":
            self.step = "lang"
            return sms_t("en", "welcome")

        if self.step == "lang":
            self.lang = "es" if low.startswith("2") or low.startswith("es") else "en"
            self.step = "hospital"
            return sms_t(self.lang, "ask_hospital")

        lang = self.lang or "en"

        if self.step == "hospital":
            row = navigator.find_hospital(self.ds, name=msg)
            if not row:
                return sms_t(lang, "hospital_not_found")
            self.row = row
            self.step = "income"
            return sms_t(lang, "ask_income")

        if self.step == "income":
            n = _int(msg)
            if n is None:
                return sms_t(lang, "ask_number")
            self.data["income"] = n
            self.step = "household"
            return sms_t(lang, "ask_household")

        if self.step == "household":
            n = _int(msg)
            if n is None or n < 1:
                return sms_t(lang, "ask_number")
            self.data["household"] = n
            self.step = "insurance"
            return sms_t(lang, "ask_insurance")

        if self.step == "insurance":
            if low not in YES and low not in NO:
                return sms_t(lang, "ask_insurance")
            self.data["insured"] = low in YES
            self.step = "collections"
            return sms_t(lang, "ask_collections")

        if self.step == "collections":
            if low not in YES and low not in NO:
                return sms_t(lang, "ask_collections")
            self.data["in_collections"] = low in YES
            self.step = "done"
            return self._plan()

        if self.step == "done":
            if low in ("letter", "carta"):
                return self._letter()
            return sms_t(lang, "plan_footer")

        return sms_t(lang, "plan_footer")

    def _intake(self):
        return {"first_name": "there", "last_name": "", "household_size": self.data["household"],
                "annual_income": self.data["income"],
                "insurance": "insured" if self.data.get("insured") else "uninsured",
                "in_collections": self.data.get("in_collections", False)}

    def _plan(self):
        lang = self.lang or "en"
        intake = self._intake()
        r = navigator.build_plan_struct(intake, self.row, lang=lang)
        self._last_plan = (intake, self.row, r)
        name = r["hospital"]["name"]
        phone = r["hospital"]["phone"]
        phone_str = (": " + phone) if phone else ""

        lines = [sms_t(lang, "plan_" + r["tier"], name=name)]
        lines.append(sms_t(lang, "plan_apply", phone=phone_str))
        if r["benefits"]:
            lines.append(sms_t(lang, "plan_benefit", benefit=_strip_md(r["benefits"][0])))
        if r["debt"]:
            lines.append(sms_t(lang, "plan_debt"))
        lines.append(sms_t(lang, "plan_letter"))
        lines.append(sms_t(lang, "plan_footer"))
        return "\n\n".join(lines)

    def _letter(self):
        lang = self.lang or "en"
        if not self._last_plan:
            return sms_t(lang, "plan_footer")
        intake, row, r = self._last_plan
        pct = navigator.fpl_percent(intake["annual_income"], intake["household_size"])
        letter = navigator.generate_letter(intake, row, pct, r["tier"])
        return sms_t(lang, "letter_sent", letter=letter)


def _int(s):
    try:
        return int(str(s).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------- Twilio webhook
def twilio_signature_ok(token, url, params, signature):
    """Validate Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted k+v)).
    `params` is a parse_qs dict (values are lists). NOTE: verify end-to-end against a real
    Twilio request before relying on it in prod — URL reconstruction behind a proxy is the
    fragile part (see deploy/GO-LIVE.md)."""
    data = url + "".join(k + (params[k][0] if params[k] else "") for k in sorted(params))
    digest = base64.b64encode(hmac.new(token.encode(), data.encode("utf-8"), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(digest, signature or "")


def make_handler(ds):
    sessions = {}

    class Handler(BaseHTTPRequestHandler):
        timeout = 15                       # so a slow/partial body can't wedge the request forever

        def log_message(self, *a):
            pass

        def _full_url(self):
            proto = self.headers.get("X-Forwarded-Proto", "https")
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
            return f"{proto}://{host}{self.path}"

        def _send_twiml(self, reply):
            twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(reply)}</Message></Response>'
            data = twiml.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path.rstrip("/") not in ("/sms", ""):
                self.send_response(404); self.end_headers(); return
            try:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (ValueError, TypeError):
                    length = 0
                length = max(0, min(length, MAX_BODY))
                form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

                token = os.environ.get("TWILIO_AUTH_TOKEN")   # off by default; the offline sim needs no token
                if token and not twilio_signature_ok(
                        token, self._full_url(), form, self.headers.get("X-Twilio-Signature")):
                    self.send_response(403); self.end_headers(); return

                frm = (form.get("From") or [""])[0].strip()
                body = (form.get("Body") or [""])[0]
                # A real Twilio request always carries From; an anonymous/malformed one gets an
                # ephemeral, un-stored session so distinct callers never collide on a shared key.
                convo = sessions.setdefault(frm, Conversation(ds)) if frm else Conversation(ds)
                reply = convo.reply(body)
            except Exception:                                 # never 500 a Twilio webhook
                reply = "Sorry — something went wrong. Reply START to try again."
            self._send_twiml(reply)

    return Handler


def serve(ds, port):
    print(f"Cobijo SMS webhook — POST http://localhost:{port}/sms  (point a Twilio number here)",
          file=sys.stderr)
    if not os.environ.get("TWILIO_AUTH_TOKEN"):
        print("  ⚠ TWILIO_AUTH_TOKEN not set — request signatures are NOT validated (dev only).",
              file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(ds)).serve_forever()


# ---------------------------------------------------------------------------- terminal simulator
def _wrap(s):
    return s.replace("\n", "\n        ")


def simulate(ds, scripted=None):
    convo = Conversation(ds)
    print("— Cobijo SMS simulator — (Ctrl-C to quit) —\n")
    print("Cobijo:", _wrap(convo.reply("")), "\n")
    if scripted is not None:                      # scripted demo: play it out and stop
        for msg in scripted:
            print("You:   ", msg)
            print("\nCobijo:", _wrap(convo.reply(msg)), "\n")
        return
    while True:                                   # interactive
        try:
            msg = input("You:    ")
        except (EOFError, KeyboardInterrupt):
            print(); return
        print("\nCobijo:", _wrap(convo.reply(msg)), "\n")


def main():
    ap = argparse.ArgumentParser(description="Cobijo Health SMS front door")
    ap.add_argument("--serve", action="store_true", help="run the Twilio webhook server")
    ap.add_argument("--demo", action="store_true", help="run a scripted demo conversation")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    if args.offline:
        navigator.USE_POLICYENGINE = False
    ds, src = navigator.load_dataset()

    if args.serve:
        return serve(ds, args.port)
    if args.demo:
        first = ds["rows"][0]["hospital"].title()
        return simulate(ds, scripted=["1", first, "38000", "4", "no", "yes", "LETTER"])
    simulate(ds)


if __name__ == "__main__":
    main()
