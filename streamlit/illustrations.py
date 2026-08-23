"""月ごとのジグソーパズル用イラスト（自前SVG、外部画像なし）。"""

import math
import random

_VIEWBOX = "0 0 700 500"


def _wrap(body, bg="#dff0f7"):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_VIEWBOX}">'
        f'<rect width="700" height="500" fill="{bg}"/>{body}</svg>'
    )


# 山下清の花火の絵を意識した、ちぎり絵風の色とりどりの点々で描く花火バースト。
# 直線のスパークではなく、大小の丸をびっしり敷き詰めて塊としての花火にする。
_FIREWORK_PALETTE = [
    "#ff3b3b", "#ff7a1a", "#ffcc33", "#ff4fa3", "#e63aa8",
    "#3aa0ff", "#38d6c0", "#7a5cff", "#ffffff", "#ffe066",
]


def _firework_burst(cx, cy, max_r, n_sparks, rng):
    # 花火＝中心から放射状に飛び散る「光の筋」の集合。同心円の点描だと菊の花に見えてしまうため、
    # 各スパークを中心から外へ伸びる筋（先端ほど明るく太い）として描く。
    dots = []
    for i in range(n_sparks):
        angle = (2 * math.pi * i / n_sparks) + rng.uniform(-0.06, 0.06)
        length = max_r * rng.uniform(0.65, 1.15)
        dx, dy = math.cos(angle), math.sin(angle)
        droop = rng.uniform(0.0, 0.18) * max(0.0, dy)  # 下向きのスパークはわずかに垂れる（尺玉らしさ）
        color = rng.choice(_FIREWORK_PALETTE)
        n_dots = max(5, int(length / 7))
        for j in range(1, n_dots + 1):
            t = j / n_dots
            r = length * t
            x = cx + dx * r
            y = cy + dy * r + droop * (t ** 2) * max_r * 0.4
            size = max(1.0, 3.6 * (1 - t) + 0.6)
            op = round(0.3 + 0.7 * (1 - t), 2)
            dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{size:.1f}" fill="{color}" opacity="{op}"/>')
        tip_x = cx + dx * length
        tip_y = cy + dy * length + droop * max_r * 0.4
        dots.append(f'<circle cx="{tip_x:.1f}" cy="{tip_y:.1f}" r="2.4" fill="#fff6d8" opacity="0.95"/>')
    # 中心の白い閃光
    dots.append(f'<circle cx="{cx}" cy="{cy}" r="{max_r*0.09:.1f}" fill="#fff6d8" opacity="0.9"/>')
    return "".join(dots)


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
    8: _wrap(  # 花火（山下清の花火の絵を意識した、色とりどりの点描バースト）
        (lambda rng: (
            _firework_burst(190, 160, 150, 46, rng)
            + _firework_burst(480, 130, 130, 40, rng)
            + _firework_burst(360, 260, 105, 34, rng)
            + _firework_burst(80, 300, 70, 22, rng)
            + _firework_burst(610, 300, 75, 22, rng)
            + '<rect x="0" y="430" width="700" height="70" fill="#0a1024"/>'
            + '<polygon points="0,430 40,430 40,395 70,395 70,430 120,430 120,410 160,410 160,430 '
            '700,430 700,500 0,500" fill="#0a1024"/>'
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
