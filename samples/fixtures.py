"""Ground-truth shipment fixtures.

Every value here is the *label* for the eval harness. The sample PDFs are
rendered FROM this dict, so `evals/golden/labels.json` can never drift from
what is actually printed on the page.

Note `incoterms: None` on SHP-2287. That is deliberate and load-bearing: a real
ocean Bill of Lading frequently carries no Incoterm at all. The only correct
extraction is null/not_found, so this fixture is our hallucination probe.
"""

CANONICAL_FIELDS = [
    "consignee_name",
    "hs_code",
    "port_of_loading",
    "port_of_discharge",
    "incoterms",
    "description_of_goods",
    "gross_weight",
    "invoice_number",
]

SHIPMENTS = {
    # ---------------------------------------------------------------- clean
    # Fully compliant with rules/acme_electronics.yaml -> expect AUTO_APPROVE
    "SHP-1042": {
        "template": "jse",
        "doc_type": "BILL_OF_LADING",
        "bol_no": "JSE-SGRTM-0098412",
        "shipper": "Sunrise Components Pte Ltd\n21 Tuas Ave 8, #03-11\nSingapore 639234",
        "consignee": "Acme Electronics Manufacturing Pte Ltd\nPrins Hendrikkade 142\n1011 AT Amsterdam, Netherlands",
        "notify": "Same as consignee",
        "vessel": "MAERSK SELETAR",
        "voyage": "241W",
        "containers": "MSKU 442810-7 / SEAL 8841207\nINV NO. INV-2026-08841\nP.O. 4500219887",
        "packages": "480",
        "description": (
            "480 CARTONS OF ELECTRONIC INTEGRATED CIRCUITS\n"
            "PROCESSORS AND CONTROLLERS\n"
            "HS CODE: 8542.31\n"
            "COUNTRY OF ORIGIN: SINGAPORE\n"
            "FREIGHT TERMS: FOB SINGAPORE"
        ),
        "gross_weight_display": "12,450.00 KGS",
        "measurement": "28.400 CBM",
        "pol": "SINGAPORE (SGSIN)",
        "pod": "ROTTERDAM (NLRTM)",
        "final_dest": "AMSTERDAM, NETHERLANDS",
        "place_date": "SINGAPORE, 14 AUG 2026",
        "originals": "THREE (3)",
        "freight": "FOB SINGAPORE\nFREIGHT COLLECT",
        "truth": {
            "consignee_name": "Acme Electronics Manufacturing Pte Ltd",
            "hs_code": "8542.31",
            "port_of_loading": "SINGAPORE (SGSIN)",
            "port_of_discharge": "ROTTERDAM (NLRTM)",
            "incoterms": "FOB",
            "description_of_goods": "ELECTRONIC INTEGRATED CIRCUITS - PROCESSORS AND CONTROLLERS",
            "gross_weight": "12450.00 KGS",
            "invoice_number": "INV-2026-08841",
        },
    },
    # ----------------------------------------------------------- discrepant
    # Different template + planted errors -> expect AMENDMENT_REQUEST
    "SHP-2287": {
        "template": "dhx",
        "doc_type": "BILL_OF_LADING",
        "bol_no": "DHX-CNRTM-7741903",
        "shipper": "Sunrise Components (Shanghai) Co Ltd\nNo. 388 Jiangchang Road\nZhabei District, Shanghai 200436, CN",
        "consignee": "Acme Electronics Mfg Pte\nPrins Hendrikkade 142\n1011 AT Amsterdam, Netherlands",
        "notify": "Acme Electronics Mfg Pte\nAttn: Import Desk",
        "vessel": "COSCO SHIPPING ARIES / 0072E / PANAMA",
        "voyage": "0072E",
        "containers": "TGHU 663219-4\nSEAL 44120983",
        "packages": "512",
        "description": (
            "512 CARTONS ELECTRONIC INTEGRATED CIRCUITS\n"
            "MEMORY MODULES - HS 8542.39"
        ),
        "gross_weight_display": "12,980 KGS",
        "measurement": "30.100 CBM",
        "pol": "SHANGHAI (CNSHA)",
        "pod": "ROTTERDAM (NLRTM)",
        "final_dest": "AMSTERDAM, NETHERLANDS",
        "place_date": "SHANGHAI, 02 SEP 2026",
        "originals": "3",
        "export_ref": "INV-2026-08847",
        # NOTE: no Incoterm anywhere on this document. On purpose.
        "truth": {
            "consignee_name": "Acme Electronics Mfg Pte",
            "hs_code": "8542.39",
            "port_of_loading": "SHANGHAI (CNSHA)",
            "port_of_discharge": "ROTTERDAM (NLRTM)",
            "incoterms": None,                      # <- must extract as not_found
            "description_of_goods": "ELECTRONIC INTEGRATED CIRCUITS - MEMORY MODULES",
            "gross_weight": "12980 KGS",
            "invoice_number": "INV-2026-08847",
        },
    },
}
