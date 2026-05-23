import requests
import json
import re
import ssl
import time
import os
import zipfile
import shutil
import argparse
import sys
import concurrent.futures
from urllib.parse import urlparse
from urllib3.exceptions import InsecureRequestWarning

# Suppress insecure warning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

class DingTalkKBExporter:
    def __init__(self, url, max_workers=5):
        self.start_url = url
        self.max_workers = max_workers
        self.session = requests.Session()
        self.session.verify = False
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://alidocs.dingtalk.com/'
        }
        self.xsrf_token = None
        self.access_token = None
        
        # Space metadata
        self.space_id = None
        self.space_name = "dingtalk_kb"
        self.root_dentry_uuid = None
        self.root_dentry_id = None
        
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
                        r = requests.get("https://alidocs.dingtalk.com/", proxies={"http": test_url, "https": test_url}, timeout=2)
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
        """Sanitize filename to be safe for Windows/Linux filesystems."""
        return re.sub(r'[\/*?:"<>|]', " ", name).strip()

    def fetch_initial_page(self):
        """Fetch start URL, extract cookies, and read space meta JSON."""
        print("1. Initializing session and cookies...")
        r = self.session.get(self.start_url, headers=self.headers, timeout=10)
        if r.status_code != 200:
            raise Exception(f"Failed to fetch initial page: HTTP {r.status_code}")
        
        cookies = self.session.cookies.get_dict()
        self.xsrf_token = cookies.get("XSRF-TOKEN")
        if not self.xsrf_token:
            raise Exception("XSRF-TOKEN cookie not found in response.")
        
        # Parse mainsite_server_content
        match = re.search(r'<script type="data" id="mainsite_server_content"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if not match:
            raise Exception("Could not find 'mainsite_server_content' tag.")
        
        server_content = json.loads(match.group(1).strip())
        
        space_info = server_content.get("spaceInfo", {}).get("data", {})
        self.space_id = space_info.get("id") or server_content.get("data", {}).get("spaceId")
        self.space_name = space_info.get("name") or "dingtalk_kb"
        self.space_name = self.clean_filename(self.space_name)
        
        self.root_dentry_uuid = space_info.get("rootDentryUuid")
        
        root_data = server_content.get("spaceRootDentry", {}).get("data", {})
        if root_data:
            self.root_dentry_id = root_data.get("dentryId")
            if not self.root_dentry_uuid:
                self.root_dentry_uuid = root_data.get("dentryUuid")
                
        print(f"Space: {self.space_name} (ID: {self.space_id})")

    def acquire_access_token(self):
        """Acquire A-TOKEN."""
        print("2. Requesting temporary access token...")
        token_url = "https://alidocs.dingtalk.com/portal/api/v1/token/getAccessToken"
        token_headers = self.headers.copy()
        token_headers.update({
            'X-XSRF-TOKEN': self.xsrf_token,
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json;charset=UTF-8',
            'Origin': 'https://alidocs.dingtalk.com',
            'Referer': self.start_url
        })
        res = self.session.post(token_url, json={}, headers=token_headers, timeout=10)
        res_json = res.json()
        if not res_json.get("isSuccess"):
            raise Exception(f"Failed to acquire token: {res_json}")
            
        self.access_token = res_json.get("data", {}).get("accessToken")
        print("Access Token successfully acquired.")

        # Setup standard list headers
        self.list_headers = self.headers.copy()
        self.list_headers.update({
            'A-TOKEN': self.access_token,
            'X-XSRF-TOKEN': self.xsrf_token,
            'Accept': 'application/json, text/plain, */*'
        })

    def fetch_dentry_info(self, uuid):
        """Fetch dentry info using UUID."""
        url = "https://alidocs.dingtalk.com/box/api/v2/dentry/info"
        params = {'dentryUuid': uuid}
        res = self.session.get(url, params=params, headers=self.list_headers, timeout=10)
        res_json = res.json()
        if not res_json.get("isSuccess"):
            raise Exception(f"dentry/info error: {res_json}")
        return res_json.get("data", {})

    def resolve_root_dentry_id(self):
        """Resolve root dentryId if missing."""
        if self.root_dentry_id:
            return
        print("3. Resolving root folder details...")
        info = self.fetch_dentry_info(self.root_dentry_uuid)
        self.root_dentry_id = info.get("dentryId")
        print(f"Resolved Root Dentry ID: {self.root_dentry_id}")

    def list_dentries(self, parent_id, load_more_id=None):
        url = "https://alidocs.dingtalk.com/box/api/v1/dentry/list"
        params = {
            'spaceId': self.space_id,
            'dentryId': parent_id,
            'pageSize': 200
        }
        if load_more_id:
            params['loadMoreId'] = load_more_id
        res = self.session.get(url, params=params, headers=self.list_headers, timeout=10)
        if res.status_code != 200:
            return None
        return res.json()

    def crawl_wiki_tree(self):
        """Crawl the entire directory structure to find all leaf pages."""
        print("4. Crawling folder hierarchy to list all pages...")
        all_pages = []
        queue = [(self.root_dentry_id, [])]
        visited_folders = set()
        
        while queue:
            curr_id, path = queue.pop(0)
            if curr_id in visited_folders:
                continue
            visited_folders.add(curr_id)
            
            has_more = True
            load_more_id = None
            
            while has_more:
                res_data = self.list_dentries(curr_id, load_more_id)
                if not res_data or not res_data.get("isSuccess"):
                    break
                    
                data = res_data.get("data", {})
                children = data.get("children", [])
                
                for child in children:
                    name = child.get("name")
                    uuid = child.get("dentryUuid")
                    child_id = child.get("dentryId")
                    dtype = child.get("dentryType")
                    has_children = child.get("hasChildren")
                    
                    item = {
                        "name": name,
                        "dentryUuid": uuid,
                        "dentryId": child_id,
                        "dentryType": dtype,
                        "parentPath": path
                    }
                    
                    if dtype == 'folder':
                        if has_children:
                            queue.append((child_id, path + [name]))
                    elif dtype == 'file':
                        all_pages.append(item)
                
                has_more = data.get("hasMore", False)
                load_more_id = data.get("loadMoreId")
                
        print(f"Tree traversal complete. Found {len(all_pages)} pages in {len(visited_folders)} folders.")
        return all_pages

    def render_ast_node(self, node):
        if isinstance(node, str):
            return node
        if not isinstance(node, list) or len(node) < 2:
            return ""
        
        ntype = node[0]
        props = node[1] or {}
        children = node[2:]
        
        rendered_children = "".join(self.render_ast_node(c) for c in children)
        
        if ntype == 'p':
            return rendered_children + "\n\n"
        elif ntype in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            level = int(ntype[1])
            return "#" * level + " " + rendered_children + "\n\n"
        elif ntype == 'img':
            src = props.get("src", "")
            name = props.get("name", "") or "image"
            return f"![{name}]({src})\n\n"
        elif ntype == 'span':
            text = rendered_children
            if props.get("bold"):
                text = f"**{text}**"
            if props.get("italic"):
                text = f"*{text}*"
            link = props.get("link")
            if link and isinstance(link, dict) and "href" in link:
                text = f"[{text}]({link['href']})"
            elif "href" in props:
                text = f"[{text}]({props['href']})"
            return text
        elif ntype in ['ul', 'ol']:
            return rendered_children + "\n"
        elif ntype == 'li':
            return f"- {rendered_children}\n"
        elif ntype == 'table':
            return "\n" + rendered_children + "\n"
        elif ntype == 'tr':
            return rendered_children + "|\n"
        elif ntype == 'td':
            return "| " + rendered_children + " "
        else:
            return rendered_children

    def fetch_page_markdown(self, page_item):
        """Fetch a single page's AST and render it to Markdown."""
        uuid = page_item["dentryUuid"]
        name = page_item["name"]
        
        try:
            # 1. Get docKey and dentryKey
            info = self.fetch_dentry_info(uuid)
            doc_key = info.get("docKey")
            dentry_key = info.get("dentryKey")
            space_id = info.get("spaceId")
            
            if not doc_key or not dentry_key:
                return name, False, "Missing keys"
                
            # 2. Get document content data
            doc_data_url = "https://alidocs.dingtalk.com/api/document/data"
            doc_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Content-Type': 'application/json',
                'A-TOKEN': self.access_token,
                'A-DENTRY-KEY': dentry_key,
                'A-DOC-KEY': doc_key,
                'A-DENTRY-UUID': uuid,
                'spaceId': space_id,
                'Cookie': "; ".join([f"{k}={v}" for k, v in self.session.cookies.get_dict().items()])
            }
            
            # Use a separate session per thread to be thread-safe (or use sessions pool)
            t_session = requests.Session()
            t_session.verify = False
            if self.proxy_url:
                t_session.proxies = {"http": self.proxy_url, "https": self.proxy_url}
                
            res = t_session.post(doc_data_url, json={"fetchBody": True}, headers=doc_headers, timeout=10)
            if res.status_code != 200:
                return name, False, f"HTTP {res.status_code}"
                
            res_json = res.json()
            if not res_json.get("isSuccess"):
                return name, False, "Request failed"
                
            checkpoint = res_json.get("data", {}).get("documentContent", {}).get("checkpoint", {})
            content_str = checkpoint.get("content", "{}")
            content_data = json.loads(content_str)
            
            main_part_id = content_data.get("main")
            main_part = content_data.get("parts", {}).get(main_part_id, {})
            body = main_part.get("data", {}).get("body", [])
            
            # 3. Render
            md_content = ""
            if len(body) > 0 and body[0] == 'root':
                for block in body[2:]:
                    md_content += self.render_ast_node(block)
            else:
                for block in body:
                    md_content += self.render_ast_node(block)
                    
            return name, True, md_content
        except Exception as e:
            return name, False, str(e)

    def run(self, output_zip_path):
        self.fetch_initial_page()
        self.acquire_access_token()
        self.resolve_root_dentry_id()
        
        pages = self.crawl_wiki_tree()
        if not pages:
            print("No pages discovered. Nothing to export.")
            return
            
        # Create temp folder for structuring
        temp_dir = "temp_kb_export"
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)
        
        print(f"\n5. Downloading and rendering {len(pages)} pages using {self.max_workers} threads...")
        
        success_count = 0
        
        # Use thread pool to speed up fetching
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Map futures
            futures = {executor.submit(self.fetch_page_markdown, p): p for p in pages}
            
            for idx, future in enumerate(concurrent.futures.as_completed(futures), 1):
                page_item = futures[future]
                name, success, result = future.result()
                
                if success:
                    success_count += 1
                    # Construct folders path
                    clean_path_parts = []
                    for p in page_item["parentPath"]:
                        part = p
                        if part.lower().endswith(".adoc"):
                            part = part[:-5]
                        clean_path_parts.append(self.clean_filename(part))
                        
                    folder_path = os.path.join(temp_dir, *clean_path_parts)
                    os.makedirs(folder_path, exist_ok=True)
                    
                    # Sanitize filename
                    display_name = name
                    if display_name.lower().endswith(".adoc"):
                        display_name = display_name[:-5]
                        
                    clean_name = self.clean_filename(display_name)
                    if not clean_name.lower().endswith(".md"):
                        clean_name += ".md"
                        
                    file_path = os.path.join(folder_path, clean_name)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(result)
                        
                    print(f" [{idx}/{len(pages)}] Successfully fetched: {'/'.join(page_item['parentPath'] + [display_name])}")
                else:
                    print(f" [{idx}/{len(pages)}] FAILED to fetch: {name} (Error: {result})")
                    
        # 6. Zip the folder
        print(f"\n6. Packaging {success_count} files into ZIP archive...")
        zip_output = output_zip_path or f"{self.space_name}.zip"
        
        # Ensure directory of zip exists
        zip_dir = os.path.dirname(zip_output)
        if zip_dir:
            os.makedirs(zip_dir, exist_ok=True)
            
        with zipfile.ZipFile(zip_output, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    # compute relative path inside zip
                    arc_name = os.path.relpath(file_path, temp_dir)
                    zipf.write(file_path, arc_name)
                    
        # Cleanup
        shutil.rmtree(temp_dir)
        print(f"\nDone! Exported {success_count} pages successfully.")
        print(f"Output ZIP file saved to: {os.path.abspath(zip_output)}")
        return zip_output

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export a DingTalk wiki/knowledge base to a ZIP file of Markdown files.")
    parser.add_argument("url", help="URL of any page inside the DingTalk knowledge base")
    parser.add_argument("-o", "--output", help="Output ZIP file name (defaults to space name)")
    parser.add_argument("-w", "--workers", type=int, default=5, help="Number of concurrent download threads (default: 5)")
    
    args = parser.parse_args()
    
    try:
        exporter = DingTalkKBExporter(args.url, max_workers=args.workers)
        exporter.run(args.output)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
