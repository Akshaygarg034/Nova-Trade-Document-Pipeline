"""Ground-truth shipment fixtures.

Every value here is the *label* for the eval harness. The sample PDFs are
rendered FROM this dict, so `evals/golden/labels.json` can never drift from
what is actually printed on the page.

Note `incoterms: None` on SHP-2287. That is deliberate and load-bearing: a real
ocean Bill of Lading frequently carries no Incoterm at all, so the only correct
extraction is not_found. It is our hallucination probe.
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
        "local_vessel": "N/A",
        "from_place": "SINGAPORE",
        "transhipment": "N/A",
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
        "total_words": "FOUR HUNDRED AND EIGHTY (480) CARTONS ONLY",
        "declared_value": "NIL",
        "freight": "FOB SINGAPORE - FREIGHT COLLECT\n\nOCEAN FREIGHT\nBUNKER ADJ. FACTOR\nDOCUMENTATION FEE\n\nTOTAL COLLECT",
        "revenue_tons": "\n\n28.400\n28.400\n1.000",
        "rate": "\n\nUSD 84.00\nUSD 12.50\nUSD 45.00",
        "per": "\n\nCBM\nCBM\nB/L",
        "collect": "\n\n2,385.60\n355.00\n45.00\n\nUSD 2,785.60",
        "ex_rate": "1.0000",
        "prepaid_at": "NIL",
        "total_prepaid": "NIL",
        "payable_at": "ROTTERDAM",
        "place_date": "SINGAPORE, 14 AUG 2026",
        "originals": "THREE (3)",
        "for_master": "AS AGENT FOR THE CARRIER\nSUNRISE SHIPPING AGENCIES PTE LTD",
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
        "booking_no": "COSU6621840",
        "quote_no": "Q-2026-4471",
        "forwarding_agent": "GLOBALINK LOGISTICS CO LTD / FMC 024419NF",
        "origin_point": "SHANGHAI, CHINA",
        "delivery_to": "Acme Electronics Mfg Pte\nDistribution Centre Unit 7\nWesthavenweg 22, 1042 AL Amsterdam, NL",
        "shipper_phone": "+86 21 6630 8890",
        "consignee_phone": "+31 20 794 2210",
        "notify_phone": "+31 20 794 2211",
        "delivery_phone": "+31 20 794 2288",
        "pre_carriage": "TRUCK",
        "place_receipt": "SHANGHAI CFS",
        "loading_pier": "YANGSHAN TERMINAL 3",
        "freight_class": "70",
        "hazmat": "N",
        "declared_value": "NIL",
        "charges": "OCEAN FREIGHT\nTERMINAL HANDLING\nDOCUMENTATION",
        "basis": "PER CBM\nPER CNTR\nPER B/L",
        "rate": "USD 78.00\nUSD 210.00\nUSD 45.00",
        "collect": "2,347.80\n210.00\n45.00",
        "total_collect": "USD 2,602.80",
        "issued_at": "SHANGHAI",
        "issue_date": "02 SEP 2026",
        "issued_by": "GLOBALINK LOGISTICS CO LTD, AS AGENT FOR THE CARRIER",
        "initials": "JW",
        # NOTE: no Incoterm anywhere in this shipment's data. On purpose.
        # The DHX template's own legal boilerplate does contain the token
        # "CIF" -- that is template text, not shipment data. See README.
        "truth": {
            "consignee_name": "Acme Electronics Mfg Pte",
            "hs_code": "8542.39",
            "port_of_loading": "SHANGHAI (CNSHA)",
            "port_of_discharge": "ROTTERDAM (NLRTM)",
            "incoterms": None,
            "description_of_goods": "ELECTRONIC INTEGRATED CIRCUITS - MEMORY MODULES",
            "gross_weight": "12980 KGS",
            "invoice_number": "INV-2026-08847",
        },
    },
}
