"""Readable page content is a compile output even when a site has no tools."""

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


def page_markdown(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select('script,style,noscript,svg,iframe,nav,footer,[role="navigation"]'):
        tag.decompose()
    root = soup.select_one('main,[role="main"],#content,#main') or soup.body or soup

    def render(tag) -> str:
        if isinstance(tag, Comment):
            return ""
        if isinstance(tag, NavigableString):
            return re.sub(r"\s+", " ", str(tag))
        if not isinstance(tag, Tag):
            return ""
        if tag.name == "pre":
            return "\n\n```\n" + tag.get_text().strip() + "\n```\n\n"
        text = "".join(render(child) for child in tag.children)
        if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            return "\n\n" + "#" * int(tag.name[1]) + " " + text.strip() + "\n\n"
        if tag.name == "a" and tag.get("href"):
            target = urljoin(url, str(tag["href"]))
            if urlsplit(target).scheme in {"http", "https", "mailto"} and text.strip():
                return f"[{text.strip()}]({target.replace(' ', '%20').replace(')', '%29')})"
        if tag.name == "img" and tag.get("alt"):
            return str(tag["alt"])
        if tag.name == "li":
            return "\n- " + text.strip() + "\n"
        if tag.name == "br":
            return "\n"
        if tag.name in {"p", "section", "article", "div", "ul", "ol", "blockquote", "tr"}:
            return "\n\n" + text.strip() + "\n\n"
        if tag.name in {"td", "th"}:
            return text.strip() + " | "
        if tag.name in {"strong", "b"}:
            return "**" + text.strip() + "**"
        if tag.name == "code":
            return "`" + text.strip() + "`"
        return text

    return re.sub(r"\n[ \t]+", "\n", re.sub(r"\n{3,}", "\n\n", render(root))).strip()[:24000]
