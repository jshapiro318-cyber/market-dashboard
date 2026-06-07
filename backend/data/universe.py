"""Stock universe for the scanner.

A curated list of ~200 highly-liquid US equities covering major sectors and
mega-cap momentum names. This avoids the Wikipedia/SP500 scrape dependency
while giving the scanner a meaningful universe to rank.
"""
from __future__ import annotations

SP500_TOP = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "AVGO", "ORCL",
    "CRM", "ADBE", "NFLX", "CSCO", "INTC", "AMD", "QCOM", "IBM", "INTU", "TXN",
    "AMAT", "MU", "LRCX", "KLAC", "ADI", "MRVL", "NOW", "PANW", "SNPS", "CDNS",
    "FTNT", "ANET", "WDAY", "DDOG", "TEAM", "SNOW", "MDB", "NET", "OKTA", "ZS",
    "PLTR", "SHOP", "UBER", "ABNB", "COIN", "ROKU", "PINS", "SNAP", "DOCU", "ZM",

    # Financials (Alpaca uses dots for class shares, e.g. BRK.B not BRK-B)
    "BRK.B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "C", "AXP",
    "SCHW", "BLK", "SPGI", "ICE", "CME", "PYPL", "XYZ", "COF", "USB", "PNC",
    "AIG", "MET", "PRU", "ALL", "TRV", "MMC", "AON", "CB", "PGR",

    # Healthcare
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT", "DHR", "BMY",
    "AMGN", "GILD", "MDT", "ISRG", "SYK", "ELV", "CI", "HUM", "CVS", "WBA",
    "REGN", "VRTX", "BIIB", "MRNA", "NVAX", "BNTX",

    # Consumer
    "WMT", "HD", "COST", "PG", "KO", "PEP", "MCD", "NKE", "SBUX", "LOW",
    "TGT", "TJX", "BKNG", "MAR", "HLT", "CMG", "YUM", "DIS", "CMCSA", "VZ",
    "T", "TMUS", "WBD", "PARA", "EA", "DKNG", "DASH", "ETSY",  # ATVI delisted (MSFT acquisition Oct 2023)

    # Industrial
    "BA", "CAT", "DE", "HON", "GE", "UPS", "FDX", "LMT", "RTX", "NOC",
    "GD", "MMM", "EMR", "ITW", "ETN", "PH", "ROP", "CSX", "UNP", "NSC",

    # Energy (PXD delisted — Pioneer acquired by Exxon May 2024)
    "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "PSX", "VLO", "OXY",
    "HES", "FANG", "DVN", "APA", "HAL", "BKR",  # MRO delisted (ConocoPhillips acquired Nov 2024)

    # Materials / Real Estate / Utilities
    "LIN", "APD", "FCX", "NEM", "SHW", "ECL", "DD", "DOW", "NUE", "STLD",
    "PLD", "AMT", "CCI", "EQIX", "DLR", "PSA", "O", "SPG", "AVB", "EQR",
    "NEE", "SO", "DUK", "AEP", "EXC", "XEL", "SRE", "D", "PCG",

    # Auto / EV / Mobility
    "F", "GM", "RIVN", "LCID", "NIO", "XPEV", "LI", "STLA",

    # Semis & other momentum
    "TSM", "ASML", "ON", "MCHP", "WOLF", "ENPH", "FSLR", "BE", "PLUG", "RUN",
    "AI", "SOUN", "SMCI", "ARM", "MARA", "RIOT", "HOOD", "RBLX",

    # Major ETFs
    "SPY", "QQQ", "DIA", "IWM", "XLF", "XLE", "XLK", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLB", "XLRE", "XLC", "GLD", "SLV", "USO", "UNG", "TLT",
    "HYG", "ARKK", "SOXX", "SMH", "TQQQ", "SQQQ", "UPRO", "SPXS", "VXX",

    # === Momentum / IPO / mid-cap expansion ===
    # Added after observing that 14/16 of June 1's biggest movers weren't in the
    # mega-cap universe. These are the names where retail momentum lives.

    # Recent IPOs (2023-2025)
    "CRWV", "NBIS", "KVYO", "RDDT", "ASTS", "ANET", "DELL", "VKTX", "RKLB",
    "BIRK", "INST", "CRGY", "CART", "KLG", "PAYO", "JOBY", "ACHR",

    # Mid-cap software / SaaS momentum
    "MDB", "HUBS", "TWLO", "MNDY", "SAIC", "DOCS", "DASH", "RBLX", "U",
    "ESTC", "DT", "GTLB", "PD", "CFLT", "FROG", "BILL", "PCTY", "PAYC",
    "ASAN", "MQ", "FIVN", "BL", "COUP", "AVID", "PEGA", "RNG", "ZI",

    # Small-cap biotech momentum
    "ERAS", "EWTX", "VKTX", "GERN", "EXEL", "MRUS", "ALNY", "BMRN", "INSM",
    "SRPT", "ARWR", "AXSM", "DNLI", "PRAX", "RXRX", "IONS", "BCRX",
    "CYTK", "ADMA", "CRSP", "EDIT", "NTLA", "BEAM", "VRTX",

    # AI / chip / hardware momentum
    "AAOI", "AVGO", "AMD", "MRVL", "ANSS", "CDNS", "SNPS", "ALAB",
    "POWL", "VST", "TLN", "CEG", "CRDO", "MPWR", "ADI",

    # Energy storage / clean tech / EV adjacent
    "FLNC", "STEM", "QS", "BLNK", "CHPT", "EVGO", "FREY", "ALB", "LAC",
    "SQM", "PLTR", "IOT", "S",

    # Housing / consumer cyclical momentum
    "TMHC", "DHI", "LEN", "PHM", "TOL", "KBH", "NVR", "MAS", "BLDR",
    "AZO", "ULTA", "RH", "WSM", "DECK", "ANF",

    # Defense / aerospace
    "LDOS", "BWXT", "KTOS", "TDG", "HEI", "AXON",

    # Telecom / international momentum
    "SKM", "TLK", "KT", "VOD", "ORAN",

    # Hospitality / casinos
    "MGM", "WYNN", "LVS", "CZR", "RCL", "CCL", "NCLH", "ABNB",

    # Other notable mid-caps that move
    "NET", "OKTA", "ZS", "CRWD", "FTNT", "PANW", "S", "PATH",
    "TOST", "AFRM", "SOFI", "UPST", "LMND", "OPEN", "RDFN",
    "CVNA", "WBA", "GME", "AMC", "BBBY", "NIO", "XPEV", "LI", "BIDU",
]


def universe(include: list[str] | None = None) -> list[str]:
    """Return the configured universe, optionally extending with extras."""
    s = list(dict.fromkeys(SP500_TOP))
    if include:
        for t in include:
            t = t.strip().upper()
            if t and t not in s:
                s.append(t)
    return s
