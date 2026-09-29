# -*- coding: utf-8 -*-
"""生成程序 icon：蓝黑对角渐变底 + 白色哥特体大写 P（多尺寸 .ico + 预览 .png）。"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(r"D:\PixivCrawler")
SIZE = 1024
FONT = r"C:\Windows\Fonts\OLDENGL.TTF"

# 1) 蓝黑对角渐变底（左上深蓝 -> 右下近黑）
base = Image.new("RGB", (SIZE, SIZE))
draw = ImageDraw.Draw(base)
top, bottom = (12, 22, 72), (3, 5, 14)
for y in range(SIZE):
    t = y / (SIZE - 1)
    draw.line([(0, y), (SIZE, y)],
              fill=(int(top[0] + (bottom[0] - top[0]) * t),
                    int(top[1] + (bottom[1] - top[1]) * t),
                    int(top[2] + (bottom[2] - top[2]) * t)))

# 2) 白色哥特体大写 P（带一点徽章式倒影感：文字下方加淡蓝 shadow 偏移）
font = ImageFont.truetype(FONT, int(SIZE * 0.80))
txt = "P"
bbox = draw.textbbox((0, 0), txt, font=font)
w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
px = (SIZE - w) // 2 - bbox[0]
py = int(SIZE * 0.55) - h // 2 - bbox[1]
draw.text((px + 14, py + 14), txt, font=font, fill=(40, 70, 140))   # shadow
draw.text((px, py), txt, font=font, fill=(255, 255, 255))            # 主体

# 3) 底部细银线装饰
ln = ImageDraw.Draw(base)
ln.rectangle([SIZE * 0.30, int(SIZE * 0.80), SIZE * 0.70, int(SIZE * 0.80) + max(4, SIZE // 120)],
             fill=(150, 165, 210))

# 4) 输出多尺寸 .ico 与预览 png
sizes = (256, 128, 64, 48, 32, 16)
imgs = [base.resize((s, s), Image.Resampling.LANCZOS) for s in sizes]
imgs[0].save(OUT / "pixiv_crawler.ico", format="ICO",
             sizes=[(s, s) for s in sizes], append_images=imgs[1:])
base.resize((512, 512), Image.Resampling.LANCZOS).save(OUT / "_icon_preview.png")
print("ico 生成:", OUT / "pixiv_crawler.ico")
print("预览图:", OUT / "_icon_preview.png")