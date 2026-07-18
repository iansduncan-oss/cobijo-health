#!/usr/bin/env python3
"""
Message catalog for the navigator — the single source of truth for every patient-facing string,
in English and Spanish.

Why a catalog: the audience is largely Spanish-speaking (the org's name, "Cobijo," is Spanish),
and the plan promises a multilingual tool. Rather than sprinkle f-strings, navigator.py and
policyengine.py render every user-facing line through `t(lang, key, **kw)`. The DATA layer
(extracted policies) stays English — translation is a UX concern only.

Templates use str.format() fields, so every interpolation and number format spec from the
original f-strings is preserved (e.g. {pct:.0f}, {income:,}). A key missing in a language falls
back to English, so a partial translation never crashes.

⚠ The Spanish strings are a working draft. Per the plan ("human-reviewed translation only for
legal/medical content"), a native/professional reviewer must sign off before launch — especially
the debt-rights and eligibility language.
"""

MESSAGES = {
    "en": {
        # --- match_charity_care ---
        "cc_free": ("You likely qualify for **FREE care**. {name} gives free/full charity care "
                    "up to {free:.0f}% of the Federal Poverty Level{upto}, and your household is "
                    "at ~{pct:.0f}%."),
        "cc_free_upto": " (free for a household of {household} earning up to ~${ceil:,}/yr)",
        "cc_discount_tier": ("You likely qualify for a **DISCOUNT**. Your ~{pct:.0f}% FPL falls in "
                             "this hospital's {lo:.0f}–{hi:.0f}% band: {how}."),
        "cc_how_basis": "discounted to {basis} rates",
        "cc_how_default": "a discounted rate",
        "cc_discount_ceiling": ("You're within {name}'s discount-eligible range (up to "
                                "{disc_ceiling:.0f}% FPL) — apply for the sliding-scale discount."),
        "cc_high_cost": ("You have insurance, but {name} has a **high-medical-cost** provision: if "
                         "your out-of-pocket bills exceed ~{thr:.0f}% of your income you may still "
                         "qualify for help. Worth applying."),
        "cc_over": ("Your income (~{pct:.0f}% FPL) is above {name}'s assistance ceiling "
                    "(~{ceiling:.0f}% FPL) — but still apply (policies changed in 2025), and use "
                    "bill-error review, negotiation, and the options below."),
        "cc_unknown": ("We couldn't read {name}'s exact income limits from its policy — but every "
                       "California hospital is required by law to offer free or discounted care. "
                       "**Apply anyway** — at ~{pct:.0f}% of the Federal Poverty Level you may well qualify."),
        "cc_no_hospital": ("We don't have this hospital's details in our directory yet — but every "
                           "California hospital is required by law to offer free or discounted care. "
                           "**Apply anyway** — at ~{pct:.0f}% of the Federal Poverty Level you may well qualify."),
        # --- statute-driven states (T4.1 Phase 2): the plan cites the state LAW, not a per-hospital FAP.
        # {law} is the statute name (kept in English, like a proper noun, across translations). ---
        "cc_statutory_free": ("Under {law}, **{name}** must provide FREE care to patients at or below "
                              "{free_pct}% of the Federal Poverty Level — and at ~{pct:.0f}%, you qualify. "
                              "Apply below."),
        "cc_statutory_discount": ("Under {law}, **{name}** must give you a discount: your ~{pct:.0f}% of the "
                                  "Federal Poverty Level falls in the {free_pct}%–{discount_pct}% range, and "
                                  "the hospital may collect no more than {cap}% of your yearly income."),
        # Cap-less variant (states that cap charges on the Medicaid rate, not a % of income — e.g. NY).
        "cc_statutory_discount_nocap": ("Under {law}, **{name}** must give you a discount: your ~{pct:.0f}% of the "
                                        "Federal Poverty Level falls in the {free_pct}%–{discount_pct}% range, so "
                                        "you qualify for reduced charges. Apply below."),
        # Discount-only variant (a state whose law sets NO free tier — every eligible patient gets a capped
        # discount, not free care — e.g. CO). No free-floor to name a band against, so it cites the ceiling.
        "cc_statutory_discount_only": ("Under {law}, **{name}** must give you a discount: at ~{pct:.0f}% of the "
                                       "Federal Poverty Level (at or below {discount_pct}%), you qualify for reduced "
                                       "charges — the hospital caps what you're charged and your monthly payments. "
                                       "Apply below."),
        "cc_statutory_over": ("Your income (~{pct:.0f}% of the Federal Poverty Level) is above the "
                              "{discount_pct}% limit {name} must offer under {law} — but apply anyway, request "
                              "a payment plan, and use the bill-error review and negotiation options below."),
        # Over-tier variant for a FREE-ONLY statute (ME: no % discount tier, so no {discount_pct} to cite —
        # the default 'over' text would render "above the None% limit"). Names the free floor instead.
        "cc_statutory_over_free_only": ("Your income (~{pct:.0f}% of the Federal Poverty Level) is above the "
                                        "{free_pct}% free-care limit {name} must offer under {law} — but apply "
                                        "anyway, request a payment plan, and use the bill-error review and "
                                        "negotiation options below."),
        # Appended to the 'over' message when the patient is above the free floor but within a statutory
        # monthly-payment cap (ME 200–400% FPL): a citeable affordability protection, not a charge discount.
        "cc_payment_cap": ("And because you're at or below {payment_cap_ceiling_pct}% of the Federal Poverty "
                           "Level, {law} caps your hospital payment plan at {payment_cap_pct}% of your monthly "
                           "income — ask the hospital to set that up."),
        # --- screen_benefits (FPL heuristic fallback) ---
        "ben_insured": ("You reported having insurance — we'd still check for cost-sharing help and "
                        "secondary programs."),
        "ben_medicaid_heuristic": ("**Medi-Cal (California Medicaid)** — at ~{pct:.0f}% FPL you're "
                                   "likely income-eligible for free or low-cost coverage that can "
                                   "even apply to recent bills. California covers many adults "
                                   "regardless of immigration status, though enrollment rules "
                                   "changed in 2026 — check your current eligibility. Using Medi-Cal "
                                   "does not count against a public-charge immigration test."),
        "ben_aca_heuristic": ("**Covered California (ACA) subsidies** — at ~{pct:.0f}% FPL you "
                              "likely qualify for premium help. (Enhanced subsidies were set to "
                              "lapse after 2025 — verify 2026 rules; PolicyEngine reflects current "
                              "law.)"),
        "ben_over_heuristic": ("Income may be above Medi-Cal/subsidy lines — we'd still check county "
                               "indigent-care and special programs."),
        # --- policyengine.benefit_leads ---
        "pe_medicaid": ("**Medi-Cal (California Medicaid)** — current CA rules (via PolicyEngine) "
                        "show your household likely eligible for free/low-cost coverage worth an "
                        "estimated ~${value:,}/yr. California covers many adults regardless of "
                        "immigration status (enrollment rules changed in 2026 — check your current "
                        "eligibility), and coverage can apply to recent bills. Using Medi-Cal does "
                        "not count against a public-charge test."),
        "pe_aca": ("**Covered California (ACA) subsidies** — PolicyEngine estimates a premium tax "
                   "credit of ~${ptc:,}/yr to lower your monthly premium on a marketplace plan."),
        "pe_over": ("Based on current CA rules (PolicyEngine), your income appears above the Medi-Cal "
                    "and marketplace-subsidy lines — we'd still check county indigent-care and "
                    "special programs."),
        # --- debt_defense ---
        "debt_rights": ("Because a bill is in collections: you have rights under the FDCPA and CA "
                        "law — you can dispute the debt and request validation."),
        "debt_retroactive": ("Charity care can often be applied **retroactively** to wipe a bill "
                             "already in collections — do that first."),
        "debt_legalaid": ("For any lawsuit or wage-garnishment threat, we'll connect you to **free "
                          "legal aid** (we don't give legal advice)."),
        "debt_credit_note": ("Note (2026): the federal rule removing medical debt from credit reports "
                             "was struck down, but bureaus still drop paid collections and those "
                             "under $500."),
        # --- build_plan ---
        "plan_greeting": "Hi {first} — here's your personalized plan for the bill from {name}.",
        "plan_greeting_anon": "Hi there — here's your personalized plan for the bill from {name}.",
        "plan_household": ("(Household of {household}, income ${income:,}/yr ≈ {pct:.0f}% of the "
                           "Federal Poverty Level.)"),
        "plan_effective": "[Based on {name}'s policy effective {date}.]",
        "plan_needs_review": ("[⚠ This hospital's data is flagged for staff verification — treat the "
                              "numbers as a strong lead, not final.]"),
        "step1": "STEP {n} — Hospital charity care (biggest lever, free to apply):",
        "step1_apply_phone": ("Apply through the hospital business office: {phone}. We'll generate "
                              "the request letter for you (below)."),
        "step1_apply_nophone": ("We'll generate the request letter for you (below) — send it to the "
                                "hospital's Financial Assistance / Business Office."),
        "step1_retroactive": ("You can apply even if the bill already arrived or went to collections "
                              "(retroactive)."),
        "step1_interest_free": ("If a balance remains, this hospital offers an **interest-free "
                                "payment plan** — ask for it by name."),
        "step2": "STEP {n} — Coverage you may qualify for going forward:",
        "step3": "STEP {n} — Protecting you from the debt:",
        "step4": ("STEP {n} — If a balance remains: bill-error review, payment plan, disease-specific "
                  "funds, and (last resort) a coached crowdfunding campaign."),
        # --- where-to-get-help routes (resources.py) — free, statewide CA destinations ---
        "res_heading": "Get free help — where to go next:",
        "res_medical": "Apply for Medi-Cal — it's free, and it can cover recent bills",
        "res_coveredca": "Get free local help enrolling in Covered California",
        "res_coverage": "Apply for free or low-cost health coverage in your state",
        "res_clinic": "Find a free or low-cost clinic near you",
        "res_legalaid": "Free legal help with medical bills, in any language",
        "letter_note_english": ("Note: the request letter below is written in English so the "
                                "hospital's billing office can process it quickly."),
        # --- bilingual reference letter: a translation shown UNDER the English letter so a non-English
        # patient can read exactly what they're sending. The English copy above is what actually gets
        # sent; this is understand-only. Law names stay in English so the reference matches the sent copy.
        "letter_ask_free": "free (fully charity) care",
        "letter_ask_discount": "a charity-care discount",
        "letter_ref_heading": "What this letter says, in your language",
        "letter_ref_warning": ("Send the English letter above — not this translation. This copy is only "
                               "so you can understand what you're sending."),
        "letter_ref_template": (
            "{name}\n{address}\n{phone}\n\n"
            "Date: {today}\n\n"
            "{hospital}\n"
            "Attn: Financial Assistance / Business Office\n"
            "Re: Request for Financial Assistance (Charity Care)\n"
            "Account/Bill #: {account}\n"
            "Date(s) of service: {service_date}\n\n"
            "To whom it may concern:\n\n"
            "I am writing to request financial assistance under your hospital's Financial Assistance "
            "Policy, as required by {law} and IRS Section 501(r). I am "
            "requesting {ask}.\n\n"
            "My household size is {household} and my annual household income is approximately "
            "${income:,}, which is about {pct:.0f}% of the Federal Poverty Level. Based on your "
            "published policy, this appears to make me eligible.\n\n"
            "Please send me your Financial Assistance application and a list of the documents you "
            "require. I am also requesting that any collection activity on the above account be paused "
            "while my application is reviewed, and that this request be applied retroactively to the "
            "balance already billed.\n\n"
            "Please also provide an itemized copy of my bill so I can review it for accuracy.\n\n"
            "Thank you for your time. You can reach me at the phone number above.\n\n"
            "Sincerely,\n{name}\n"),
        # --- result hero headlines (structured web UI) ---
        "result_free": "Good news — you likely qualify for FREE care",
        "result_discount": "You likely qualify for a discount",
        "result_high_cost": "You may qualify based on high medical costs",
        "result_over": "You may be over the income limit — but you still have options",
        "result_unknown": "We couldn't confirm this hospital's limits — but you should still apply",
    },
    "es": {
        # --- match_charity_care ---
        "cc_free": ("Usted probablemente califica para **ATENCIÓN GRATUITA**. {name} ofrece "
                    "atención de caridad gratuita/completa hasta el {free:.0f}% del Nivel Federal "
                    "de Pobreza{upto}, y su hogar está en ~{pct:.0f}%."),
        "cc_free_upto": " (gratis para un hogar de {household} que gane hasta ~${ceil:,}/año)",
        "cc_discount_tier": ("Usted probablemente califica para un **DESCUENTO**. Su ~{pct:.0f}% del "
                             "Nivel Federal de Pobreza cae en el rango de {lo:.0f}–{hi:.0f}% de este hospital: {how}."),
        "cc_how_basis": "con descuento a tarifas de {basis}",
        "cc_how_default": "una tarifa con descuento",
        "cc_discount_ceiling": ("Usted está dentro del rango que puede recibir descuento de {name} (hasta "
                                "{disc_ceiling:.0f}% del Nivel Federal de Pobreza) — solicite el descuento de escala móvil."),
        "cc_high_cost": ("Usted tiene seguro, pero {name} tiene un beneficio para **gastos médicos "
                         "altos**: si sus gastos de bolsillo superan ~{thr:.0f}% de sus ingresos, "
                         "aún podría calificar para recibir ayuda. Vale la pena solicitarla."),
        "cc_over": ("Sus ingresos (~{pct:.0f}% del Nivel Federal de Pobreza) están por encima del límite de asistencia de "
                    "{name} (~{ceiling:.0f}% del Nivel Federal de Pobreza) — pero solicítela de todos modos (las políticas "
                    "cambiaron en 2025), y use la revisión de errores en la factura, la negociación y "
                    "las opciones a continuación."),
        "cc_unknown": ("No pudimos leer los límites de ingresos exactos de {name} en su política — pero "
                       "todo hospital de California está obligado por ley a ofrecer atención gratuita o "
                       "con descuento. **Solicítela de todos modos** — con ~{pct:.0f}% del Nivel Federal "
                       "de Pobreza es muy posible que califique."),
        "cc_no_hospital": ("Todavía no tenemos los detalles de este hospital en nuestro directorio, pero "
                           "todo hospital de California está obligado por ley a ofrecer atención gratuita o "
                           "con descuento. **Solicítela de todos modos** — con ~{pct:.0f}% del Nivel Federal "
                           "de Pobreza es muy posible que califique."),
        # --- estados con ley propia (T4.1 Phase 2): {law} = nombre de la ley (se mantiene en inglés) ---
        "cc_statutory_free": ("Según {law}, **{name}** debe brindar atención GRATUITA a pacientes con ingresos "
                              "iguales o inferiores al {free_pct}% del Nivel Federal de Pobreza — y con "
                              "~{pct:.0f}%, usted califica. Solicítela abajo."),
        "cc_statutory_discount": ("Según {law}, **{name}** debe otorgarle un descuento: su ~{pct:.0f}% del Nivel "
                                  "Federal de Pobreza está en el rango de {free_pct}%–{discount_pct}%, y el "
                                  "hospital no puede cobrar más del {cap}% de su ingreso anual."),
        "cc_statutory_discount_only": ("Según {law}, **{name}** debe otorgarle un descuento: con ~{pct:.0f}% del "
                                       "Nivel Federal de Pobreza (en o por debajo del {discount_pct}%), usted califica "
                                       "para cargos reducidos: el hospital limita lo que le cobran y sus pagos "
                                       "mensuales. Solicítelo a continuación."),
        "cc_statutory_discount_nocap": ("Según {law}, **{name}** debe otorgarle un descuento: su ~{pct:.0f}% del "
                                        "Nivel Federal de Pobreza está en el rango de {free_pct}%–{discount_pct}%, "
                                        "por lo que usted califica para cargos reducidos. Solicítela abajo."),
        "cc_statutory_over": ("Sus ingresos (~{pct:.0f}% del Nivel Federal de Pobreza) superan el límite del "
                              "{discount_pct}% que {name} debe ofrecer según {law} — pero solicítela de todos "
                              "modos, pida un plan de pago y use las opciones de revisión de errores en la "
                              "factura y negociación a continuación."),
        "cc_statutory_over_free_only": ("Sus ingresos (~{pct:.0f}% del Nivel Federal de Pobreza) superan el "
                                        "límite del {free_pct}% de atención gratuita que {name} debe ofrecer "
                                        "según {law} — pero solicítela de todos modos, pida un plan de pago y "
                                        "use las opciones de revisión de errores en la factura y negociación abajo."),
        "cc_payment_cap": ("Y como usted está en o por debajo del {payment_cap_ceiling_pct}% del Nivel Federal "
                           "de Pobreza, {law} limita su plan de pago hospitalario al {payment_cap_pct}% de sus "
                           "ingresos mensuales — pida al hospital que lo establezca."),
        # --- screen_benefits (FPL heuristic fallback) ---
        "ben_insured": ("Usted indicó que tiene seguro — de todos modos revisaríamos ayuda con los "
                        "gastos compartidos y programas secundarios."),
        "ben_medicaid_heuristic": ("**Medi-Cal (Medicaid de California)** — con ~{pct:.0f}% del Nivel Federal de Pobreza "
                                   "usted probablemente es elegible por ingresos para cobertura "
                                   "gratuita o de bajo costo, que puede aplicarse a facturas "
                                   "recientes. California cubre a muchos adultos sin importar su "
                                   "estatus migratorio, aunque las reglas de inscripción cambiaron "
                                   "en 2026 — verifique su elegibilidad actual. Usar Medi-Cal no "
                                   "cuenta en su contra en la prueba migratoria de carga pública."),
        "ben_aca_heuristic": ("**Covered California (subsidios de ACA)** — con ~{pct:.0f}% del Nivel Federal de Pobreza "
                              "usted probablemente califica para ayuda con la prima. (Los subsidios "
                              "ampliados iban a expirar después de 2025 — verifique las reglas de "
                              "2026; PolicyEngine refleja la ley vigente.)"),
        "ben_over_heuristic": ("Sus ingresos pueden estar por encima de los límites de Medi-Cal o de "
                               "los subsidios — de todos modos revisaríamos la atención para "
                               "indigentes del condado y programas especiales."),
        # --- policyengine.benefit_leads ---
        "pe_medicaid": ("**Medi-Cal (Medicaid de California)** — según las reglas vigentes de "
                        "California (vía PolicyEngine), su hogar probablemente es elegible para "
                        "cobertura gratuita o de bajo costo con un valor estimado de ~${value:,}/año. "
                        "California cubre a muchos adultos sin importar su estatus migratorio (las "
                        "reglas de inscripción cambiaron en 2026 — verifique su elegibilidad actual), "
                        "y la cobertura puede aplicarse a facturas recientes. Usar Medi-Cal no cuenta "
                        "en su contra en la prueba de carga pública."),
        "pe_aca": ("**Covered California (subsidios de ACA)** — PolicyEngine estima un crédito "
                   "tributario para la prima de ~${ptc:,}/año para reducir su prima mensual en un "
                   "plan del mercado."),
        "pe_over": ("Según las reglas vigentes de California (vía PolicyEngine), sus ingresos parecen "
                    "estar por encima de los límites de Medi-Cal y de los subsidios del mercado — de "
                    "todos modos revisaríamos la atención para indigentes del condado y programas "
                    "especiales."),
        # --- debt_defense ---
        "debt_rights": ("Como una factura está en cobranza: usted tiene derechos bajo la FDCPA y la "
                        "ley de California — puede disputar la deuda y solicitar su validación."),
        "debt_retroactive": ("La atención de caridad a menudo puede aplicarse de forma "
                             "**retroactiva** para eliminar una factura que ya está en cobranza — "
                             "haga eso primero."),
        "debt_legalaid": ("Ante cualquier demanda o amenaza de embargo de salario, lo conectaremos "
                          "con **asistencia legal gratuita** (nosotros no damos asesoría legal)."),
        "debt_credit_note": ("Nota (2026): la regla federal que eliminaba la deuda médica de los "
                             "reportes de crédito fue anulada, pero los burós aún retiran las "
                             "cobranzas pagadas y las menores de $500."),
        # --- build_plan ---
        "plan_greeting": "Hola {first} — aquí está su plan personalizado para la factura de {name}.",
        "plan_greeting_anon": "Hola — aquí está su plan personalizado para la factura de {name}.",
        "plan_household": ("(Hogar de {household}, ingreso de ${income:,}/año ≈ {pct:.0f}% del Nivel "
                           "Federal de Pobreza.)"),
        "plan_effective": "[Basado en la política de {name} vigente desde {date}.]",
        "plan_needs_review": ("[⚠ Los datos de este hospital están marcados para verificación del "
                              "personal — trate los números como una pista sólida, no como algo "
                              "definitivo.]"),
        "step1": "PASO {n} — Atención caritativa del hospital (la mayor palanca, gratis de solicitar):",
        "step1_apply_phone": ("Solicite a través de la oficina administrativa del hospital: {phone}. "
                              "Le generaremos la carta de solicitud (abajo)."),
        "step1_apply_nophone": ("Le generaremos la carta de solicitud (abajo) — envíela a la Oficina "
                                "de Asistencia Financiera / Administrativa del hospital."),
        "step1_retroactive": ("Puede solicitarla incluso si la factura ya llegó o pasó a cobranza "
                              "(retroactivo)."),
        "step1_interest_free": ("Si queda un saldo, este hospital ofrece un **plan de pago sin "
                                "intereses** — pídalo por su nombre."),
        "step2": "PASO {n} — Cobertura para la que podría calificar en adelante:",
        "step3": "PASO {n} — Cómo protegerlo de la deuda:",
        "step4": ("PASO {n} — Si queda un saldo: revisión de errores en la factura, plan de pago, "
                  "fondos para enfermedades específicas y (último recurso) una campaña de "
                  "recaudación con acompañamiento."),
        # --- rutas de ayuda (resources.py) — recursos gratuitos y estatales de California ---
        "res_heading": "Obtenga ayuda gratuita — a dónde acudir:",
        "res_medical": "Solicite Medi-Cal — es gratis y puede cubrir facturas recientes",
        "res_coveredca": "Reciba ayuda local gratuita para inscribirse en Covered California",
        "res_coverage": "Solicite cobertura de salud gratuita o de bajo costo en su estado",
        "res_clinic": "Encuentre una clínica gratuita o de bajo costo cerca de usted",
        "res_legalaid": "Ayuda legal gratuita con facturas médicas, en cualquier idioma",
        "letter_note_english": ("Nota: la carta de solicitud a continuación está escrita en inglés "
                                "para que la oficina de facturación del hospital pueda procesarla "
                                "rápidamente."),
        # --- carta de referencia bilingüe (traducción mostrada DEBAJO de la carta en inglés) ---
        "letter_ask_free": "atención gratuita (totalmente caritativa)",
        "letter_ask_discount": "un descuento por atención de caridad",
        "letter_ref_heading": "Lo que dice esta carta, en su idioma",
        "letter_ref_warning": ("Envíe la carta en inglés de arriba, no esta traducción. Esta copia es "
                               "solo para que usted entienda lo que está enviando."),
        "letter_ref_template": (
            "{name}\n{address}\n{phone}\n\n"
            "Fecha: {today}\n\n"
            "{hospital}\n"
            "Atención: Asistencia Financiera / Oficina Administrativa\n"
            "Asunto: Solicitud de Asistencia Financiera (Atención de Caridad)\n"
            "N.º de cuenta/factura: {account}\n"
            "Fecha(s) de servicio: {service_date}\n\n"
            "A quien corresponda:\n\n"
            "Escribo para solicitar asistencia financiera conforme a la Política de Asistencia "
            "Financiera de su hospital, según lo exige la {law} y la "
            "Sección 501(r) del IRS. Solicito {ask}.\n\n"
            "El tamaño de mi hogar es {household} y mi ingreso familiar anual es de aproximadamente "
            "${income:,}, lo que equivale a cerca del {pct:.0f}% del Nivel Federal de Pobreza. Según "
            "su política publicada, esto parece hacerme elegible.\n\n"
            "Por favor, envíeme su solicitud de Asistencia Financiera y una lista de los documentos que "
            "requiere. También solicito que se suspenda cualquier gestión de cobro sobre la cuenta "
            "anterior mientras se revisa mi solicitud, y que esta solicitud se aplique retroactivamente "
            "al saldo ya facturado.\n\n"
            "Por favor, proporcióneme también una copia detallada de mi factura para poder revisar su "
            "exactitud.\n\n"
            "Gracias por su tiempo. Puede comunicarse conmigo al número de teléfono indicado arriba.\n\n"
            "Atentamente,\n{name}\n"),
        # --- result hero headlines (structured web UI) ---
        "result_free": "Buenas noticias — usted probablemente califica para atención GRATUITA",
        "result_discount": "Usted probablemente califica para un descuento",
        "result_high_cost": "Podría calificar por sus altos costos médicos",
        "result_over": "Es posible que supere el límite de ingresos, pero aún tiene opciones",
        "result_unknown": "No pudimos confirmar los límites de este hospital — pero aún así debería solicitarla",
    },
}

# The other 8 languages (zh/vi/tl/ko/hy/fa/ar/ru) load their catalog from the "plan" section of
# web/i18n/<lang>.json (machine-assisted, native-review pending). en/es stay inline above so the
# CLI and the unit tests never depend on the web/ tree. t() falls back to English per missing key.
import json as _json
import os as _os
_I18N_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "web", "i18n")
for _lg in ("zh", "vi", "tl", "ko", "hy", "fa", "ar", "ru"):
    _p = _os.path.join(_I18N_DIR, f"{_lg}.json")
    try:
        _plan = _json.load(open(_p, encoding="utf-8")).get("plan")
        if _plan:
            MESSAGES[_lg] = _plan
    except (FileNotFoundError, ValueError):
        pass


def t(lang, key, **kw):
    """Render a catalog string. Falls back to English if the language or key is missing."""
    table = MESSAGES.get(lang) or MESSAGES["en"]
    template = table.get(key)
    if template is None:
        template = MESSAGES["en"][key]
    return template.format(**kw)
