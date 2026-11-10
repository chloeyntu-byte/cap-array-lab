"""Product-side font fallback for Tk builds returning lower-case family names."""
from tkinter import font as tkfont, ttk


def apply_theme(root, fw):
    fw.apply(root,product='cap')
    have={name.lower():name for name in tkfont.families(root)}
    family=next((have[name.lower()] for name in ('Inter','Noto Sans','Liberation Sans','DejaVu Sans') if name.lower() in have),None)
    if family is None:return
    for name in ('TkDefaultFont','TkTextFont','TkMenuFont','TkHeadingFont','TkCaptionFont','TkSmallCaptionFont','TkTooltipFont'):
        try:tkfont.nametofont(name).configure(family=family)
        except Exception:pass
    for key,value in root.fw_fonts.items():
        if key!='mono':root.fw_fonts[key]=(family,)+value[1:]
    style=ttk.Style(root)
    style.configure('.',font=(family,10))
    style.configure('Primary.TButton',font=(family,10,'bold'))
    style.configure('Title.TLabel',font=(family,14,'bold'))
    style.configure('Section.TLabel',font=(family,9,'bold'))
    style.configure('Treeview.Heading',font=(family,9,'bold'))
