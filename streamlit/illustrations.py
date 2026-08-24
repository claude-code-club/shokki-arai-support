"""月ごとのジグソーパズル用イラスト（自前SVG、外部画像なし）。"""

import math
import random

_VIEWBOX = "0 0 700 500"


def _wrap(body, bg="#dff0f7"):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_VIEWBOX}">'
        f'<rect width="700" height="500" fill="{bg}"/>{body}</svg>'
    )


# 山下清の花火の絵を意識した、素朴で幾何学的なスタイル。
# 写真のような不揃いな光跡ではなく、正円に近い輪郭・まっすぐな放射スパーク・ベタ塗りの2色構成で描く。
_FIREWORK_PALETTES = {
    "gold": ("#ffcc33", "#ffe066"),
    "pink": ("#ff4fa3", "#ffffff"),
    "blue": ("#3aa0ff", "#ffffff"),
    "red": ("#ff3b3b", "#ffcc33"),
    "purple": ("#7a5cff", "#3aa0ff"),
}


def _firework_burst(cx, cy, r, n_sparks, palette, rng):
    main_color, center_color = _FIREWORK_PALETTES[palette]
    parts = []
    # まっすぐなスパークで正円の輪郭を作る（山下清の花火絵の特徴）
    for i in range(n_sparks):
        angle = 2 * math.pi * i / n_sparks
        spark_r = r * rng.uniform(0.94, 1.0)
        x = cx + spark_r * math.cos(angle)
        y = cy + spark_r * math.sin(angle)
        width = rng.uniform(1.1, 1.7)
        parts.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" stroke="{main_color}" stroke-width="{width:.1f}" opacity="0.92"/>')
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rng.uniform(1.6,2.4):.1f}" fill="{main_color}" opacity="0.95"/>')
    # フチを縁取る点線の輪（散った火花のふちどり）
    n_ring = int(n_sparks * 1.4)
    for i in range(n_ring):
        angle = 2 * math.pi * i / n_ring
        rr = r * rng.uniform(1.0, 1.06)
        x = cx + rr * math.cos(angle)
        y = cy + rr * math.sin(angle)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.1" fill="{main_color}" opacity="0.6"/>')
    # 中心の明るい光
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r*0.22:.1f}" fill="{center_color}" opacity="0.9"/>')
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r*0.045:.1f}" fill="#fff6d8" opacity="0.95"/>')
    return "".join(parts)


def _skyline(base_y, rng):
    # べた塗りシルエットの素朴な街並み（窓の明かり付き）
    parts = []
    x = 0.0
    while x < 700:
        w = rng.uniform(26, 56)
        h = rng.uniform(22, 72)
        top = base_y - h
        parts.append(f'<rect x="{x:.0f}" y="{top:.0f}" width="{w:.0f}" height="{h:.0f}" fill="#0a1024"/>')
        for _ in range(rng.randint(1, 4)):
            if rng.random() < 0.6:
                wx = x + rng.uniform(4, max(5, w - 8))
                wy = top + rng.uniform(4, max(5, h - 8))
                parts.append(f'<rect x="{wx:.0f}" y="{wy:.0f}" width="3" height="4" fill="#ffcc66" opacity="{rng.uniform(0.5,0.9):.2f}"/>')
        x += w + rng.uniform(2, 8)
    return "".join(parts)


def _water_reflection(top_y, rng):
    # 花火の色を水面にうっすら映す
    parts = [f'<rect x="0" y="{top_y}" width="700" height="{500-top_y}" fill="#0d2350"/>']
    colors = ["#ffcc33", "#ff4fa3", "#3aa0ff", "#ff3b3b", "#7a5cff"]
    for _ in range(20):
        x = rng.uniform(0, 700)
        y0 = top_y + rng.uniform(2, 8)
        length = rng.uniform(10, 30)
        color = rng.choice(colors)
        parts.append(f'<line x1="{x:.0f}" y1="{y0:.0f}" x2="{x:.0f}" y2="{y0+length:.0f}" stroke="{color}" stroke-width="1.2" opacity="{rng.uniform(0.15,0.35):.2f}"/>')
    return "".join(parts)


MONTH_SVGS = {
    1: _wrap(  # 正月・松
        '<circle cx="580" cy="90" r="60" fill="#e8543c"/>'
        '<polygon points="120,420 200,180 280,420" fill="#2f6b3a"/>'
        '<polygon points="90,420 170,240 250,420" fill="#357c41"/>'
        '<rect x="150" y="420" width="80" height="40" fill="#8a5a2b"/>',
        bg="#fdf1d6",
    ),
    2: _wrap(  # 節分・バレンタイン
        '<path d="M180,220 C180,160 260,160 260,220 C260,270 180,320 180,360 '
        'C180,320 100,270 100,220 C100,160 180,160 180,220 Z" fill="#e0567a"/>'
        '<path d="M480,260 C480,210 550,210 550,260 C550,300 480,340 480,370 '
        'C480,340 410,300 410,260 C410,210 480,210 480,260 Z" fill="#c23a63"/>'
        '<circle cx="350" cy="120" r="30" fill="#8a5a2b"/>',
        bg="#fbe4ec",
    ),
    3: _wrap(  # ひな祭り
        '<polygon points="220,420 220,260 340,420" fill="#e0567a"/>'
        '<circle cx="270" cy="230" r="30" fill="#ffe3c2"/>'
        '<polygon points="420,420 420,260 300,420" fill="#3f5fa0"/>'
        '<circle cx="390" cy="230" r="30" fill="#ffe3c2"/>'
        '<circle cx="150" cy="150" r="16" fill="#f6a5c0"/>'
        '<circle cx="550" cy="180" r="16" fill="#f6a5c0"/>',
        bg="#fdeef2",
    ),
    4: _wrap(  # 桜
        '<rect x="330" y="220" width="30" height="220" fill="#6b4a34"/>'
        '<g fill="#f6b8ce">'
        '<circle cx="300" cy="180" r="45"/><circle cx="380" cy="170" r="50"/>'
        '<circle cx="250" cy="220" r="40"/><circle cx="430" cy="220" r="40"/>'
        '<circle cx="340" cy="140" r="42"/>'
        '</g>'
        '<circle cx="120" cy="330" r="8" fill="#f6b8ce"/>'
        '<circle cx="560" cy="300" r="8" fill="#f6b8ce"/>'
        '<circle cx="480" cy="400" r="8" fill="#f6b8ce"/>',
        bg="#eaf4fb",
    ),
    5: _wrap(  # こどもの日・鯉のぼり
        '<rect x="340" y="60" width="12" height="380" fill="#8a5a2b"/>'
        '<path d="M352,90 L560,110 L520,140 L560,170 L352,190 Z" fill="#2f6b8a"/>'
        '<path d="M352,200 L520,215 L490,240 L520,265 L352,280 Z" fill="#e0567a"/>'
        '<path d="M352,290 L470,300 L448,320 L470,340 L352,350 Z" fill="#e8a93c"/>',
        bg="#dff0f7",
    ),
    6: _wrap(  # 梅雨・紫陽花
        '<g fill="#7c8fe0">'
        '<circle cx="300" cy="250" r="18"/><circle cx="330" cy="230" r="18"/>'
        '<circle cx="360" cy="250" r="18"/><circle cx="330" cy="270" r="18"/>'
        '<circle cx="330" cy="250" r="18"/>'
        '</g>'
        '<g fill="#9aa8f2">'
        '<circle cx="420" cy="300" r="16"/><circle cx="445" cy="285" r="16"/>'
        '<circle cx="470" cy="300" r="16"/><circle cx="445" cy="315" r="16"/>'
        '</g>'
        '<line x1="150" y1="120" x2="130" y2="180" stroke="#8fa6e0" stroke-width="4"/>'
        '<line x1="200" y1="140" x2="180" y2="200" stroke="#8fa6e0" stroke-width="4"/>'
        '<line x1="600" y1="130" x2="580" y2="190" stroke="#8fa6e0" stroke-width="4"/>',
        bg="#e4ecf9",
    ),
    7: _wrap(  # 七夕
        '<rect x="330" y="60" width="16" height="400" fill="#5a8a4a"/>'
        '<polygon points="330,90 300,120 330,110 330,140 360,120" fill="#7cbf64" opacity="0.7"/>'
        '<polygon points="330,180 300,210 330,200 330,230 360,210" fill="#7cbf64" opacity="0.7"/>'
        '<rect x="380" y="150" width="20" height="80" fill="#e8a93c"/>'
        '<rect x="420" y="180" width="20" height="80" fill="#e0567a"/>'
        '<rect x="460" y="160" width="20" height="80" fill="#3f5fa0"/>'
        '<polygon points="150,80 158,100 178,100 162,112 168,132 150,120 132,132 138,112 122,100 142,100" fill="#f4d35e"/>',
        bg="#1e2a55",
    ),
    8: _wrap(  # 花火（山下清の花火絵を意識した、正円・直線スパーク・ベタ塗り2色のスタイル）
        (lambda rng: (
            "".join(
                f'<circle cx="{rng.uniform(20,680):.0f}" cy="{rng.uniform(20,420):.0f}" r="{rng.uniform(0.6,1.4):.1f}" fill="#ffffff" opacity="{rng.uniform(0.3,0.7):.2f}"/>'
                for _ in range(60)
            )
            + _firework_burst(190, 160, 150, 56, "gold", rng)
            + _firework_burst(480, 130, 130, 48, "pink", rng)
            + _firework_burst(360, 260, 105, 40, "blue", rng)
            + _firework_burst(80, 300, 70, 28, "red", rng)
            + _firework_burst(610, 300, 75, 28, "purple", rng)
            + _water_reflection(455, rng)
            + _skyline(455, rng)
        ))(random.Random(20260808)),
        bg="#0c1b3a",
    ),
    9: _wrap(  # お月見
        '<circle cx="540" cy="120" r="60" fill="#f4e6a1"/>'
        '<circle cx="200" cy="380" r="22" fill="#f5e3c0"/>'
        '<circle cx="244" cy="380" r="22" fill="#f5e3c0"/>'
        '<circle cx="222" cy="342" r="22" fill="#f5e3c0"/>'
        '<path d="M100,440 Q120,380 100,340" stroke="#c9b877" stroke-width="3" fill="none"/>'
        '<path d="M130,440 Q150,370 130,330" stroke="#c9b877" stroke-width="3" fill="none"/>',
        bg="#1c2748",
    ),
    10: _wrap(  # 紅葉・ハロウィン
        '<path d="M580,300 C560,260 580,220 600,200 C620,220 640,260 620,300 '
        'C640,300 660,320 640,340 C660,350 650,380 620,370 C630,400 600,410 590,380 '
        'C570,410 540,390 550,360 C520,360 520,330 550,320 C530,300 560,280 580,300 Z" fill="#e07a2f"/>'
        '<circle cx="220" cy="330" r="70" fill="#e8862c"/>'
        '<rect x="205" y="250" width="30" height="35" fill="#4a7a3a"/>',
        bg="#f7e2c4",
    ),
    11: _wrap(  # 紅葉
        '<path d="M300,320 C280,280 300,240 320,220 C340,240 360,280 340,320 '
        'C360,320 380,340 360,360 C380,370 370,400 340,390 C350,420 320,430 310,400 '
        'C290,430 260,410 270,380 C240,380 240,350 270,340 C250,320 280,300 300,320 Z" fill="#c94f2b"/>'
        '<path d="M480,280 C460,240 480,200 500,180 C520,200 540,240 520,280 '
        'C540,280 560,300 540,320 C560,330 550,360 520,350 C530,380 500,390 490,360 '
        'C470,390 440,370 450,340 C420,340 420,310 450,300 C430,280 460,260 480,280 Z" fill="#e08a2f"/>',
        bg="#f9e8d2",
    ),
    12: _wrap(  # クリスマス
        '<polygon points="350,80 300,180 400,180" fill="#2f6b3a"/>'
        '<polygon points="350,150 285,260 415,260" fill="#2f6b3a"/>'
        '<polygon points="350,230 270,360 430,360" fill="#2f6b3a"/>'
        '<rect x="330" y="360" width="40" height="40" fill="#6b4a34"/>'
        '<circle cx="350" cy="90" r="12" fill="#f4d35e"/>'
        '<circle cx="150" cy="120" r="6" fill="#ffffff"/><circle cx="200" cy="200" r="6" fill="#ffffff"/>'
        '<circle cx="550" cy="140" r="6" fill="#ffffff"/><circle cx="500" cy="220" r="6" fill="#ffffff"/>'
        '<circle cx="100" cy="300" r="6" fill="#ffffff"/><circle cx="600" cy="300" r="6" fill="#ffffff"/>',
        bg="#16203f",
    ),
}


def get_month_svg(month):
    return MONTH_SVGS.get(month, _wrap('<circle cx="350" cy="250" r="80" fill="#9aa8f2"/>'))
