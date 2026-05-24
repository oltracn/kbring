import os
import re
import json
import uuid
import time
import urllib.parse
import zipfile
import shutil
import argparse
import sys
import concurrent.futures
from pathlib import Path
from typing import Dict, Tuple, List
import requests
from requests import Response, exceptions as req_exc
from urllib3.exceptions import InsecureRequestWarning

# Suppress insecure warning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

class YuqueKBExporter:
    def __init__(self, book_url, max_workers=5, download_images=True):
        self.book_url = book_url
        self.max_workers = max_workers
        self.download_images = download_images
        self.session = requests.Session()
        self.session.verify = False
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        }
        self.session.headers.update(self.headers)
        
        # Proxy settings
        self.proxy_url = None
        self.detect_proxy()

    def detect_proxy(self):
        """Automatically detect Clash proxy on host to bypass WSL network timeouts."""
        # 1. Try config file first
        if os.path.exists("proxy_config.json"):
            try:
                with open("proxy_config.json", "r") as f:
                    cfg = json.load(f)
                    self.proxy_url = cfg.get("proxy")
                    if self.proxy_url:
                        print(f"Loaded proxy from config: {self.proxy_url}")
                        self.session.proxies = {"http": self.proxy_url, "https": self.proxy_url}
                        return
            except Exception:
                pass

        # 2. Try default WSL host gateway IP and ports
        try:
            res = os.popen("ip route | grep default").read()
            m = re.search(r'default via ([\d\.]+)', res)
            if m:
                host_ip = m.group(1)
                for port in [7890, 7897]:
                    test_url = f"http://{host_ip}:{port}"
                    try:
                        r = requests.get("https://www.yuque.com/", proxies={"http": test_url, "https": test_url}, timeout=2)
                        if r.status_code == 200:
                            self.proxy_url = test_url
                            self.session.proxies = {"http": self.proxy_url, "https": self.proxy_url}
                            print(f"Auto-detected working host proxy: {self.proxy_url}")
                            return
                    except Exception:
                        pass
        except Exception:
            pass

    def clean_filename(self, name):
        """Sanitize filename/folder name to be safe for Windows/Linux filesystems."""
        # Replace forbidden chars with underscore
        cleaned = re.sub(r'[\/:*?"<>|\r\n]', "_", name)
        # Avoid multiple consecutive underscores
        cleaned = re.sub(r'_+', '_', cleaned)
        return cleaned.strip()

    def extract_fallback_title(self, md_content: str) -> str:
        """Extract the first non-empty, non-heading-marker line from markdown body as a fallback title."""
        for line in md_content.splitlines():
            line = line.strip()
            # Strip leading markdown heading markers
            line = re.sub(r'^#+\s*', '', line)
            if line:
                return line
        return "Untitled"

    def fetch_book_metadata(self) -> dict:
        """Fetch the book homepage and extract JavaScript decoded metadata."""
        print(f"1. Fetching book page: {self.book_url} ...")
        resp = self.session.get(self.book_url, timeout=15)
        resp.raise_for_status()

        matches = re.findall(r'decodeURIComponent\(\"(.+)\"\)\);', resp.text)
        if not matches:
            raise Exception("Failed to find book data block inside the page HTML.")
        
        try:
            metadata = json.loads(urllib.parse.unquote(matches[0]))
            return metadata
        except json.JSONDecodeError as e:
            raise Exception(f"Failed to parse decoded metadata JSON: {e}")

    def _extract_images(self, md: str) -> List[Tuple[str, str]]:
        """Extract markdown images ![alt](url) and HTML <img src="url">."""
        pattern_md = re.compile(r'!\[([^\]]*)\]\((https?[^)]+)\)', re.IGNORECASE)
        pattern_html = re.compile(r'<img[^>]*?src=["\'](https?[^"\']+)["\']', re.IGNORECASE)

        images = pattern_md.findall(md)
        images += [('', m) for m in pattern_html.findall(md)]
        return images

    def _download_image_with_retry(self, img_url: str, dest_path: Path, retries=3) -> bool:
        """Download image with retries, using thread-local session proxies."""
        # Setup local request params
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(img_url, headers=self.headers, proxies=proxies, verify=False, timeout=10, stream=True)
                if resp.status_code == 200:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def clean_html_tags(self, text: str) -> str:
        """Strip styling HTML tags while preserving their plain text content."""
        if not text:
            return ""
        # Convert break tags to newlines
        text = re.sub(r'<br\s*/?>', '\n', text)
        
        # Remove Yuque block alerts (e.g. :::danger, :::info, :::)
        text = re.sub(r'^:::.*$', '', text, flags=re.MULTILINE)
        
        # Remove font and span tags completely, keeping internal text
        text = re.sub(r'</?font[^>]*>', '', text)
        text = re.sub(r'</?span[^>]*>', '', text)
        
        # Remove empty anchor tags like <a name="..."></a>
        text = re.sub(r'<a\s+name="[^"]*"\s*>\s*</a>', '', text)
        text = re.sub(r'<a\s+name="[^"]*"\s*/?>', '', text)
        
        # Clean up empty markdown bold tags that might result
        text = re.sub(r'\*\*\*\*+', '', text)
        
        return text

    def clean_non_code_blocks(self, text: str) -> str:
        """Find markdown code blocks, check if they contain actual programming code, and strip backticks if they are just plain text."""
        if not text:
            return ""
            
        def is_actual_code(lang: str, content: str) -> bool:
            lang = lang.strip().lower()
            code_langs = {'python', 'py', 'javascript', 'js', 'typescript', 'ts', 'html', 'css', 'sql', 'java', 'c', 'cpp', 'c++', 'go', 'rust', 'php', 'ruby', 'bash', 'shell', 'sh', 'yaml', 'yml', 'json', 'xml', 'dockerfile', 'makefile', 'ini'}
            if lang in code_langs:
                return True
                
            # If language is plain text or not specified
            if lang in ('', 'text', 'plain', 'plaintext'):
                # Check JSON
                stripped = content.strip()
                if (stripped.startswith('{') and stripped.endswith('}')) or (stripped.startswith('[') and stripped.endswith(']')):
                    try:
                        json.loads(stripped)
                        return True
                    except Exception:
                        pass
                
                # Check for programming keywords
                code_indicators = [
                    r'\bdef\s+\w+\s*\(',
                    r'\bfunction\b',
                    r'\bimport\s+[\w\*{]',
                    r'\bconst\s+\w+\s*=',
                    r'\bvar\s+\w+\s*=',
                    r'\blet\s+\w+\s*=',
                    r'#include\s+<',
                    r'\bpublic\s+class\b',
                    r'\bselect\s+.*\s+from\b',
                    r'</?\w+(?:\s+[^>]*)?>',
                    r'\{\s*"\w+"\s*:',
                ]
                for pattern in code_indicators:
                    if re.search(pattern, content, re.IGNORECASE):
                        return True
                
                # Count syntax symbols
                code_symbols = content.count(';') + content.count('{') + content.count('}') + content.count('(') + content.count(')')
                lines_count = content.count('\n') + 1
                has_chinese = bool(re.search(r'[\u4e00-\u9fff]', content))
                if has_chinese and (code_symbols / lines_count) < 0.5:
                    return False
                    
            return True

        # Regex to find code blocks: ```[lang]\n[content]```
        pattern = re.compile(r'```(\w*)\n([\s\S]*?)\n```')
        
        def replacer(match):
            lang = match.group(1)
            content = match.group(2)
            if not is_actual_code(lang, content):
                return content
            return match.group(0)
            
        return pattern.sub(replacer, text)

    def fetch_and_save_page(self, book_id: str, slug: str, item_title: str, dest_md_path: Path, images_dir: Path) -> Tuple[str, bool, str]:
        """Fetch a single page's markdown, download its images, and write to disk."""
        api_url = f"https://www.yuque.com/api/docs/{slug}?book_id={book_id}&merge_dynamic_data=false&mode=markdown"
        
        # Use thread-local configurations for proxy
        t_session = requests.Session()
        t_session.verify = False
        if self.proxy_url:
            t_session.proxies = {"http": self.proxy_url, "https": self.proxy_url}
        
        try:
            resp = t_session.get(api_url, headers=self.headers, timeout=15)
            if resp.status_code != 200:
                return item_title, False, f"HTTP {resp.status_code}"
            
            res_json = resp.json()
            md_content = res_json.get("data", {}).get("sourcecode", "")
            
            # Clean HTML styling tags
            md_content = self.clean_html_tags(md_content)
            # Clean non-code blocks
            md_content = self.clean_non_code_blocks(md_content)
            
            # Determine effective title, falling back to first body line if empty/generic
            effective_title = item_title.strip() if item_title else ""
            if not effective_title or effective_title.lower() in ('untitled', 'unnamed', 'noname'):
                effective_title = self.extract_fallback_title(md_content)

            # Prepend document title as Level-1 Heading if not already present
            title_header = f"# {effective_title}"
            if not md_content or not md_content.lstrip().startswith(title_header):
                md_content = f"# {effective_title}\n\n" + (md_content or "")
            
            # Handle image downloads if enabled
            if self.download_images and md_content:
                images = self._extract_images(md_content)
                images_map = {} # remote -> local relative path
                
                for alt, img_url in images:
                    if img_url in images_map:
                        continue
                    
                    # Resolve suffix
                    parsed_url = urllib.parse.urlparse(img_url)
                    suffix = Path(parsed_url.path).suffix.lower()
                    if not suffix or len(suffix) > 6:
                        suffix = ".png"
                        
                    # Deterministic image filename using MD5 hash of URL
                    import hashlib
                    img_hash = hashlib.md5(img_url.encode('utf-8')).hexdigest()
                    img_filename = f"{img_hash}{suffix}"
                    img_dest = images_dir / img_filename
                    
                    # Compute relative path from the page folder to the global images dir
                    depth = len(dest_md_path.parent.relative_to(images_dir.parent).parts)
                    rel_prefix = "../" * depth if depth > 0 else ""
                    local_rel_path = f"{rel_prefix}images/{img_filename}"
                    
                    success = self._download_image_with_retry(img_url, img_dest)
                    if success and img_dest.exists() and img_dest.stat().st_size > 0:
                        images_map[img_url] = local_rel_path
                    else:
                        if img_dest.exists():
                            try:
                                img_dest.unlink()
                            except Exception:
                                pass
                        images_map[img_url] = img_url # fallback to remote
                
                # Replace inside markdown
                for remote, local in images_map.items():
                    md_content = md_content.replace(remote, local)
            
            # Save file
            dest_md_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_md_path, "w", encoding="utf-8") as f:
                f.write(md_content)
                
            return item_title, True, "Success"
            
        except Exception as e:
            return item_title, False, str(e)

    def run(self, output_zip_path=None):
        # 1. Fetch metadata
        try:
            metadata = self.fetch_book_metadata()
        except Exception as e:
            print(f"Fatal error fetching metadata: {e}", file=sys.stderr)
            return None

        book_data = metadata.get("book", {})
        book_id = str(book_data.get("id"))
        book_name = book_data.get("name") or "yuque_kb"
        book_name = self.clean_filename(book_name)
        toc = book_data.get("toc", [])
        
        print(f"Book: {book_name} (ID: {book_id})")
        print(f"Total TOC items: {len(toc)}")
        
        if not toc:
            print("No items found in TOC. Exiting.")
            return None
            
        # 2. Build folder structure mapping
        # uuid -> (title, parent_uuid)
        uuid_title_parent = {
            d["uuid"]: (d["title"], d["parent_uuid"]) for d in toc
        }
        
        resolved_paths = {} # uuid -> relative path
        
        def resolve_path(u: str) -> str:
            if u in resolved_paths:
                return resolved_paths[u]
            if u not in uuid_title_parent:
                return ""
            title, parent = uuid_title_parent[u]
            safe_title = self.clean_filename(title)
            if not parent:
                path_ = safe_title
            else:
                path_ = f"{resolve_path(parent)}/{safe_title}"
            resolved_paths[u] = path_
            return path_

        # Prepare workspace paths
        temp_dir = Path("temp_yuque_export")
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        images_dir = temp_dir / "images"
        if self.download_images:
            images_dir.mkdir(parents=True, exist_ok=True)

        summary_lines = [f"# {book_name}\n"]
        download_queue = []
        
        # 3. Process TOC
        for item in toc:
            uuid_ = item.get("uuid")
            path_rel = resolve_path(uuid_)
            if not path_rel:
                continue
                
            is_dir = item.get("type") == "TITLE" or (item.get("child_uuid") and item.get("child_uuid") != "")
            
            # Write structured directories
            if is_dir:
                (temp_dir / path_rel).mkdir(parents=True, exist_ok=True)
                header_level = path_rel.count("/") + 2 # ## for top level
                summary_lines.append("#" * header_level + f" {path_rel.split('/')[-1]}")
            
            # If it contains document URL, queue it for downloading
            if item.get("url"):
                if is_dir:
                    folder_name = path_rel.split('/')[-1]
                    md_filename = f"{path_rel}/{folder_name}.md"
                else:
                    md_filename = f"{path_rel}.md"
                summary_indent = "  " * path_rel.count("/")
                summary_lines.append(f"{summary_indent}* [{item['title']}]({urllib.parse.quote(md_filename)})")
                
                download_queue.append({
                    "slug": item["url"],
                    "title": item["title"],
                    "dest_path": temp_dir / md_filename
                })
                
        # 4. Run concurrent downloads
        print(f"\n2. Fetching {len(download_queue)} documents with {self.max_workers} threads...")
        success_count = 0
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self.fetch_and_save_page,
                    book_id,
                    q["slug"],
                    q["title"],
                    q["dest_path"],
                    images_dir
                ): q for q in download_queue
            }
            
            for idx, future in enumerate(concurrent.futures.as_completed(futures), 1):
                queue_item = futures[future]
                title, success, status = future.result()
                if success:
                    success_count += 1
                    rel_p = queue_item["dest_path"].relative_to(temp_dir)
                    print(f" [{idx}/{len(download_queue)}] Successfully fetched: {rel_p}")
                else:
                    print(f" [{idx}/{len(download_queue)}] FAILED to fetch: {title} (Error: {status})")

        # 5. Write SUMMARY.md
        with open(temp_dir / "SUMMARY.md", "w", encoding="utf-8") as f_sum:
            f_sum.write("\n".join(summary_lines))
        print(f"\n3. SUMMARY.md generated.")
        
        # 6. ZIP packaging
        zip_output = output_zip_path or f"{book_name}.zip"
        print(f"4. Packaging files into ZIP: {zip_output} ...")
        
        zip_dir = os.path.dirname(zip_output)
        if zip_dir:
            os.makedirs(zip_dir, exist_ok=True)
            
        with zipfile.ZipFile(zip_output, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_name = os.path.relpath(file_path, temp_dir)
                    zipf.write(file_path, arc_name)
                    
        # Cleanup
        shutil.rmtree(temp_dir)
        print(f"\nExport complete! {success_count} documents written.")
        print(f"Output ZIP file saved to: {os.path.abspath(zip_output)}")
        return zip_output

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export a Yuque knowledge base/book to a ZIP of Markdown files with local images.")
    parser.add_argument("url", help="URL of the Yuque knowledge base (e.g. https://www.yuque.com/qucfgq/bailing)")
    parser.add_argument("-o", "--output", help="Output ZIP file name (defaults to book name)")
    parser.add_argument("-w", "--workers", type=int, default=5, help="Number of concurrent download threads (default: 5)")
    parser.add_argument("--no-images", action="store_true", help="Disable downloading images locally (keeps remote URLs)")
    
    args = parser.parse_args()
    
    try:
        exporter = YuqueKBExporter(args.url, max_workers=args.workers, download_images=not args.no_images)
        exporter.run(args.output)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
