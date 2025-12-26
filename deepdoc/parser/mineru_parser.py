#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from io import BytesIO
from os import PathLike
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Optional

import numpy as np
import pdfplumber
import requests
from PIL import Image
from strenum import StrEnum

from deepdoc.parser.pdf_parser import RAGFlowPdfParser

LOCK_KEY_pdfplumber = "global_shared_lock_pdfplumber"
if LOCK_KEY_pdfplumber not in sys.modules:
    sys.modules[LOCK_KEY_pdfplumber] = threading.Lock()


class MinerUContentType(StrEnum):
    """
    MinerU 识别的内容类型枚举
    定义了 MinerU 可以识别的各种文档元素类型
    """
    IMAGE = "image"  # 图片
    TABLE = "table"  # 表格
    TEXT = "text"  # 文本
    EQUATION = "equation"  # 公式
    CODE = "code"  # 代码
    LIST = "list"  # 列表
    DISCARDED = "discarded"  # 丢弃的内容


class MinerUParser(RAGFlowPdfParser):
    """
    MinerU PDF 解析器类
    封装了与 MinerU 工具的交互，支持通过可执行文件或 API 调用 MinerU 进行 PDF 解析
    MinerU 是一个强大的 PDF 解析工具，能够识别文档布局、提取文本、表格、图片等
    """
    def __init__(self, mineru_path: str = "mineru", mineru_api: str = "http://host.docker.internal:9987", mineru_server_url: str = ""):
        """
        初始化 MinerU 解析器
        
        Args:
            mineru_path: MinerU 可执行文件的路径，默认为 "mineru"（需要在 PATH 中）
            mineru_api: MinerU API 服务器的地址，用于通过 HTTP API 调用
            mineru_server_url: VLM 服务器的 URL（用于 vlm-http-client 后端）
        """
        self.mineru_path = Path(mineru_path)  # MinerU 可执行文件路径
        self.mineru_api = mineru_api.rstrip("/")  # API 地址（去除末尾斜杠）
        self.mineru_server_url = mineru_server_url.rstrip("/")  # VLM 服务器地址
        self.using_api = False  # 是否使用 API 模式（而非可执行文件模式）
        self.outlines = []  # PDF 大纲（目录）信息
        self.logger = logging.getLogger(self.__class__.__name__)  # 日志记录器

    def _extract_zip_no_root(self, zip_path, extract_to, root_dir):
        """
        解压 ZIP 文件，但去除根目录层级
        例如：如果 ZIP 文件结构是 root_dir/file.txt，解压后变成 extract_to/file.txt
        
        Args:
            zip_path: ZIP 文件路径
            extract_to: 解压目标目录
            root_dir: 要跳过的根目录名称（如果为 None，会自动检测）
        """
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            # 如果没有指定根目录，尝试自动检测
            if not root_dir:
                files = zip_ref.namelist()
                # 如果第一个文件是目录，则作为根目录
                if files and files[0].endswith("/"):
                    root_dir = files[0]
                else:
                    root_dir = None

            # 如果没有根目录或根目录格式不正确，直接解压所有文件
            if not root_dir or not root_dir.endswith("/"):
                self.logger.info(f"[MinerU] No root directory found, extracting all...fff{root_dir}")
                zip_ref.extractall(extract_to)
                return

            # 去除根目录层级，将所有文件解压到目标目录
            root_len = len(root_dir)
            for member in zip_ref.infolist():
                filename = member.filename
                # 跳过根目录本身
                if filename == root_dir:
                    self.logger.info("[MinerU] Ignore root folder...")
                    continue

                # 去除根目录前缀
                path = filename
                if path.startswith(root_dir):
                    path = path[root_len:]

                # 构建完整的目标路径
                full_path = os.path.join(extract_to, path)
                if member.is_dir():
                    # 如果是目录，创建目录
                    os.makedirs(full_path, exist_ok=True)
                else:
                    # 如果是文件，创建父目录并写入文件
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    with open(full_path, "wb") as f:
                        f.write(zip_ref.read(filename))

    def _is_http_endpoint_valid(self, url, timeout=5):
        try:
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            return response.status_code in [200, 301, 302, 307, 308]
        except Exception:
            return False

    def check_installation(self, backend: str = "pipeline", server_url: Optional[str] = None) -> tuple[bool, str]:
        """
        检查 MinerU 是否已正确安装或 API 是否可访问
        
        Args:
            backend: MinerU 后端类型
                - "pipeline": 标准管道后端
                - "vlm-http-client": 使用 HTTP 客户端连接 VLM 服务器
                - "vlm-transformers": 使用 Transformers 库的 VLM
                - "vlm-vllm-engine": 使用 vLLM 引擎的 VLM
            server_url: VLM 服务器 URL（用于 vlm-http-client）
        
        Returns:
            tuple[bool, str]: (是否可用, 错误原因)
        """
        reason = ""

        # 验证后端类型是否有效
        valid_backends = ["pipeline", "vlm-http-client", "vlm-transformers", "vlm-vllm-engine"]
        if backend not in valid_backends:
            reason = "[MinerU] Invalid backend '{backend}'. Valid backends are: {valid_backends}"
            logging.warning(reason)
            return False, reason

        subprocess_kwargs = {
            "capture_output": True,
            "text": True,
            "check": True,
            "encoding": "utf-8",
            "errors": "ignore",
        }

        if platform.system() == "Windows":
            subprocess_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        if server_url is None:
            server_url = self.mineru_server_url

        if backend == "vlm-http-client" and server_url:
            try:
                server_accessible = self._is_http_endpoint_valid(server_url + "/openapi.json")
                logging.info(f"[MinerU] vlm-http-client server check: {server_accessible}")
                if server_accessible:
                    self.using_api = False  # We are using http client, not API
                    return True, reason
                else:
                    reason = f"[MinerU] vlm-http-client server not accessible: {server_url}"
                    logging.warning(f"[MinerU] vlm-http-client server not accessible: {server_url}")
                    return False, reason
            except Exception as e:
                logging.warning(f"[MinerU] vlm-http-client server check failed: {e}")
                try:
                    response = requests.get(server_url, timeout=5)
                    logging.info(f"[MinerU] vlm-http-client server connection check: success with status {response.status_code}")
                    self.using_api = False
                    return True, reason
                except Exception as e:
                    reason = f"[MinerU] vlm-http-client server connection check failed: {server_url}: {e}"
                    logging.warning(f"[MinerU] vlm-http-client server connection check failed: {server_url}: {e}")
                    return False, reason

        try:
            result = subprocess.run([str(self.mineru_path), "--version"], **subprocess_kwargs)
            version_info = result.stdout.strip()
            if version_info:
                logging.info(f"[MinerU] Detected version: {version_info}")
            else:
                logging.info("[MinerU] Detected MinerU, but version info is empty.")
            return True, reason
        except subprocess.CalledProcessError as e:
            logging.warning(f"[MinerU] Execution failed (exit code {e.returncode}).")
        except FileNotFoundError:
            logging.warning("[MinerU] MinerU not found. Please install it via: pip install -U 'mineru[core]'")
        except Exception as e:
            logging.error(f"[MinerU] Unexpected error during installation check: {e}")

        # If executable check fails, try API check
        try:
            if self.mineru_api:
                # check openapi.json
                openapi_exists = self._is_http_endpoint_valid(self.mineru_api + "/openapi.json")
                if not openapi_exists:
                    reason = "[MinerU] Failed to detect vaild MinerU API server"
                    return openapi_exists, reason
                logging.info(f"[MinerU] Detected {self.mineru_api}/openapi.json: {openapi_exists}")
                self.using_api = openapi_exists
                return openapi_exists, reason
            else:
                logging.info("[MinerU] api not exists.")
        except Exception as e:
            reason = f"[MinerU] Unexpected error during api check: {e}"
            logging.error(f"[MinerU] Unexpected error during api check: {e}")
        return False, reason

    def _run_mineru(
        self, input_path: Path, output_dir: Path, method: str = "auto", backend: str = "pipeline", lang: Optional[str] = None, server_url: Optional[str] = None, callback: Optional[Callable] = None
    ):
        """
        运行 MinerU 解析 PDF 文件
        根据配置选择使用 API 模式或可执行文件模式
        
        Args:
            input_path: PDF 文件路径
            output_dir: 输出目录
            method: 解析方法（"auto"/"manual"/"paper"）
            backend: 后端类型
            lang: 语言设置
            server_url: VLM 服务器 URL
            callback: 进度回调函数
        """
        # 根据是否使用 API 选择不同的执行方式
        if self.using_api:
            # 通过 HTTP API 调用 MinerU 服务
            self._run_mineru_api(input_path, output_dir, method, backend, lang, callback)
        else:
            # 直接调用 MinerU 可执行文件
            self._run_mineru_executable(input_path, output_dir, method, backend, lang, server_url, callback)

    def _run_mineru_api(self, input_path: Path, output_dir: Path, method: str = "auto", backend: str = "pipeline", lang: Optional[str] = None, callback: Optional[Callable] = None):
        output_zip_path = os.path.join(str(output_dir), "output.zip")

        pdf_file_path = str(input_path)

        if not os.path.exists(pdf_file_path):
            raise RuntimeError(f"[MinerU] PDF file not exists: {pdf_file_path}")

        pdf_file_name = Path(pdf_file_path).stem.strip()
        output_path = os.path.join(str(output_dir), pdf_file_name, method)
        os.makedirs(output_path, exist_ok=True)

        files = {"files": (pdf_file_name + ".pdf", open(pdf_file_path, "rb"), "application/pdf")}

        data = {
            "output_dir": "./output",
            "lang_list": lang,
            "backend": backend,
            "parse_method": method,
            "formula_enable": True,
            "table_enable": True,
            "server_url": None,
            "return_md": True,
            "return_middle_json": True,
            "return_model_output": True,
            "return_content_list": True,
            "return_images": True,
            "response_format_zip": True,
            "start_page_id": 0,
            "end_page_id": 99999,
        }

        headers = {"Accept": "application/json"}
        try:
            self.logger.info(f"[MinerU] invoke api: {self.mineru_api}/file_parse")
            if callback:
                callback(0.20, f"[MinerU] invoke api: {self.mineru_api}/file_parse")
            response = requests.post(url=f"{self.mineru_api}/file_parse", files=files, data=data, headers=headers, timeout=1800)

            response.raise_for_status()
            if response.headers.get("Content-Type") == "application/zip":
                self.logger.info(f"[MinerU] zip file returned, saving to {output_zip_path}...")

                if callback:
                    callback(0.30, f"[MinerU] zip file returned, saving to {output_zip_path}...")

                with open(output_zip_path, "wb") as f:
                    f.write(response.content)

                self.logger.info(f"[MinerU] Unzip to {output_path}...")
                self._extract_zip_no_root(output_zip_path, output_path, pdf_file_name + "/")

                if callback:
                    callback(0.40, f"[MinerU] Unzip to {output_path}...")
            else:
                self.logger.warning("[MinerU] not zip returned from api：%s " % response.headers.get("Content-Type"))
        except Exception as e:
            raise RuntimeError(f"[MinerU] api failed with exception {e}")
        self.logger.info("[MinerU] Api completed successfully.")

    def _run_mineru_executable(
        self, input_path: Path, output_dir: Path, method: str = "auto", backend: str = "pipeline", lang: Optional[str] = None, server_url: Optional[str] = None, callback: Optional[Callable] = None
    ):
        cmd = [str(self.mineru_path), "-p", str(input_path), "-o", str(output_dir), "-m", method]
        if backend:
            cmd.extend(["-b", backend])
        if lang:
            cmd.extend(["-l", lang])
        if server_url and backend == "vlm-http-client":
            cmd.extend(["-u", server_url])

        self.logger.info(f"[MinerU] Running command: {' '.join(cmd)}")

        subprocess_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "ignore",
            "bufsize": 1,
        }

        if platform.system() == "Windows":
            subprocess_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        process = subprocess.Popen(cmd, **subprocess_kwargs)
        stdout_queue, stderr_queue = Queue(), Queue()

        def enqueue_output(pipe, queue, prefix):
            for line in iter(pipe.readline, ""):
                if line.strip():
                    queue.put((prefix, line.strip()))
            pipe.close()

        threading.Thread(target=enqueue_output, args=(process.stdout, stdout_queue, "STDOUT"), daemon=True).start()
        threading.Thread(target=enqueue_output, args=(process.stderr, stderr_queue, "STDERR"), daemon=True).start()

        while process.poll() is None:
            for q in (stdout_queue, stderr_queue):
                try:
                    while True:
                        prefix, line = q.get_nowait()
                        if prefix == "STDOUT":
                            self.logger.info(f"[MinerU] {line}")
                        else:
                            self.logger.warning(f"[MinerU] {line}")
                except Empty:
                    pass
            time.sleep(0.1)

        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"[MinerU] Process failed with exit code {return_code}")
        self.logger.info("[MinerU] Command completed successfully.")

    def __images__(self, fnm, zoomin: int = 1, page_from=0, page_to=600, callback=None):
        """
        加载 PDF 页面并转换为图片
        
        Args:
            fnm: PDF 文件路径或二进制数据
            zoomin: 缩放因子（分辨率 = 72 * zoomin DPI）
            page_from: 起始页码（从 0 开始）
            page_to: 结束页码（不包含）
            callback: 进度回调函数
        """
        self.page_from = page_from  # 记录起始页码
        self.page_to = page_to  # 记录结束页码
        try:
            # 使用 pdfplumber 打开 PDF 文件
            with pdfplumber.open(fnm) if isinstance(fnm, (str, PathLike)) else pdfplumber.open(BytesIO(fnm)) as pdf:
                self.pdf = pdf
                # 将每个页面转换为图片（PIL.Image 对象）
                # resolution: 分辨率（DPI），antialias: 抗锯齿
                self.page_images = [p.to_image(resolution=72 * zoomin, antialias=True).original for _, p in enumerate(self.pdf.pages[page_from:page_to])]
        except Exception as e:
            # 如果加载失败，设置为 None
            self.page_images = None
            self.total_page = 0
            logging.exception(e)

    def _line_tag(self, bx):
        """
        生成位置标签字符串
    
        步骤：
        1. 获取页码（从 page_idx 转换，从 0 开始 → 从 1 开始）
        2. 获取边界框坐标（bbox）
        3. 如果已加载页面图片，将相对坐标（0-1000）转换为绝对像素坐标
        4. 生成位置标签字符串
        """
        pn = [bx["page_idx"] + 1]  # 页码（从 1 开始）
        positions = bx.get("bbox", (0, 0, 0, 0))  # 边界框 (x0, top, x1, bottom)
        x0, top, x1, bott = positions

        # 如果已加载页面图片，将相对坐标（0-1000）转换为绝对像素坐标
        if hasattr(self, "page_images") and self.page_images and len(self.page_images) > bx["page_idx"]:
            page_width, page_height = self.page_images[bx["page_idx"]].size
            # MinerU 使用 0-1000 的相对坐标，需要转换为像素坐标
            x0 = (x0 / 1000.0) * page_width
            x1 = (x1 / 1000.0) * page_width
            top = (top / 1000.0) * page_height
            bott = (bott / 1000.0) * page_height

        # 生成位置标签字符串
        return "@@{}\t{:.1f}\t{:.1f}\t{:.1f}\t{:.1f}##".format("-".join([str(p) for p in pn]), x0, x1, top, bott)

    def crop(self, text, ZM=1, need_position=False):
        """
        根据位置标签从 PDF 页面中裁剪图片
    
        步骤：
        1. 提取位置信息
        2. 检查页面图片是否已加载
        3. 过滤无效位置
        4. 扩展裁剪区域（添加上下文）
        5. 裁剪每个位置的图片片段
        6. 垂直拼接所有片段
        """
        imgs = []  # 存储裁剪的图片片段
        # 从文本中提取位置信息
        poss = self.extract_positions(text)
        if not poss:
            # 如果没有位置信息，返回 None
            if need_position:
                return None, None
            return

        # 检查是否已加载页面图片
        if not getattr(self, "page_images", None):
            self.logger.warning("[MinerU] crop called without page images; skipping image generation.")
            if need_position:
                return None, None
            return

        page_count = len(self.page_images)  # PDF 总页数

        # 过滤位置信息：移除无效的页码索引
        filtered_poss = []
        for pns, left, right, top, bottom in poss:
            if not pns:
                self.logger.warning("[MinerU] Empty page index list in crop; skipping this position.")
                continue
            # 只保留有效的页码索引（在页面范围内）
            valid_pns = [p for p in pns if 0 <= p < page_count]
            if not valid_pns:
                self.logger.warning(f"[MinerU] All page indices {pns} out of range for {page_count} pages; skipping.")
                continue
            filtered_poss.append((valid_pns, left, right, top, bottom))

        poss = filtered_poss
        if not poss:
            # 如果过滤后没有有效位置，返回 None
            self.logger.warning("[MinerU] No valid positions after filtering; skip cropping.")
            if need_position:
                return None, None
            return

        # 计算所有位置中的最大宽度（用于统一裁剪宽度）
        max_width = max(np.max([right - left for (_, left, right, _, _) in poss]), 6)
        GAP = 6  # 图片片段之间的间距（像素）
        
        # 在第一个位置之前添加一个扩展区域（向上扩展 120 像素，用于包含可能的上下文）
        pos = poss[0]
        first_page_idx = pos[0][0]
        poss.insert(0, ([first_page_idx], pos[1], pos[2], max(0, pos[3] - 120), max(pos[3] - GAP, 0)))
        
        # 在最后一个位置之后添加一个扩展区域（向下扩展 120 像素）
        pos = poss[-1]
        last_page_idx = pos[0][-1]
        if not (0 <= last_page_idx < page_count):
            self.logger.warning(f"[MinerU] Last page index {last_page_idx} out of range for {page_count} pages; skipping crop.")
            if need_position:
                return None, None
            return
        last_page_height = self.page_images[last_page_idx].size[1]
        poss.append(
            (
                [last_page_idx],
                pos[1],
                pos[2],
                min(last_page_height, pos[4] + GAP),  # 从底部位置 + GAP 开始
                min(last_page_height, pos[4] + 120),  # 到 底部位置 + 120 结束
            )
        )

        # 裁剪每个位置的图片片段
        positions = []  # 存储位置信息（如果需要）
        for ii, (pns, left, right, top, bottom) in enumerate(poss):
            # 统一使用最大宽度
            right = left + max_width

            # 如果底部小于等于顶部，设置一个最小高度
            if bottom <= top:
                bottom = top + 2

            # 如果位置跨越多页，需要累加前面页面的高度
            for pn in pns[1:]:
                if 0 <= pn - 1 < page_count:
                    bottom += self.page_images[pn - 1].size[1]
                else:
                    self.logger.warning(f"[MinerU] Page index {pn}-1 out of range for {page_count} pages during crop; skipping height accumulation.")

            # 检查基础页码是否有效
            if not (0 <= pns[0] < page_count):
                self.logger.warning(f"[MinerU] Base page index {pns[0]} out of range for {page_count} pages during crop; skipping this segment.")
                continue

            # 裁剪第一页的图片片段
            img0 = self.page_images[pns[0]]
            x0, y0, x1, y1 = int(left), int(top), int(right), int(min(bottom, img0.size[1]))
            crop0 = img0.crop((x0, y0, x1, y1))
            imgs.append(crop0)
            # 记录位置信息（不包括首尾的扩展区域）
            if 0 < ii < len(poss) - 1:
                positions.append((pns[0] + self.page_from, x0, x1, y0, y1))

            # 如果跨页，需要裁剪后续页面的图片
            bottom -= img0.size[1]  # 减去第一页的高度
            for pn in pns[1:]:
                if not (0 <= pn < page_count):
                    self.logger.warning(f"[MinerU] Page index {pn} out of range for {page_count} pages during crop; skipping this page.")
                    continue
                page = self.page_images[pn]
                # 从页面顶部开始裁剪
                x0, y0, x1, y1 = int(left), 0, int(right), int(min(bottom, page.size[1]))
                cimgp = page.crop((x0, y0, x1, y1))
                imgs.append(cimgp)
                # 记录位置信息
                if 0 < ii < len(poss) - 1:
                    positions.append((pn + self.page_from, x0, x1, y0, y1))
                bottom -= page.size[1]  # 减去当前页的高度

        # 如果没有裁剪到任何图片，返回 None
        if not imgs:
            if need_position:
                return None, None
            return

        # 计算合并后图片的总高度和宽度
        height = 0
        for img in imgs:
            height += img.size[1] + GAP  # 每个图片片段的高度 + 间距
        height = int(height)
        width = int(np.max([i.size[0] for i in imgs]))  # 最大宽度
        
        # 创建新的空白图片（浅灰色背景）
        pic = Image.new("RGB", (width, height), (245, 245, 245))
        height = 0
        # 将所有图片片段垂直拼接
        for ii, img in enumerate(imgs):
            # 对首尾的扩展区域添加半透明遮罩（表示这是上下文区域，不是原始内容）
            if ii == 0 or ii + 1 == len(imgs):
                img = img.convert("RGBA")
                overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                overlay.putalpha(128)  # 50% 透明度
                img = Image.alpha_composite(img, overlay).convert("RGB")
            # 将图片片段粘贴到合并图片上
            pic.paste(img, (0, int(height)))
            height += img.size[1] + GAP  # 更新下一个片段的起始位置

        if need_position:
            return pic, positions
        return pic

    @staticmethod
    def extract_positions(txt: str):
        """
        从文本中提取位置标签
    
        步骤：
        1. 使用正则表达式查找所有位置标签
        2. 解析每个标签，提取页码和坐标
        3. 转换为标准格式
        """
        poss = []
        # 使用正则表达式查找所有位置标签
        for tag in re.findall(r"@@[0-9-]+\t[0-9.\t]+##", txt):
            # 解析标签：去除 @@ 和 ##，按制表符分割
            pn, left, right, top, bottom = tag.strip("#").strip("@").split("\t")
            # 转换为浮点数
            left, right, top, bottom = float(left), float(right), float(top), float(bottom)
            # 解析页码（支持范围，如 "1-3"）
            # 页码从 1 开始，转换为从 0 开始的索引
            poss.append(([int(p) - 1 for p in pn.split("-")], left, right, top, bottom))
        return poss

    def _read_output(self, output_dir: Path, file_stem: str, method: str = "auto", backend: str = "pipeline") -> list[dict[str, Any]]:
        """
        读取 MinerU 的输出 JSON 文件
    
        步骤：
        1. 构建候选路径列表（根据 backend 和 method）
        2. 在候选路径中查找 JSON 文件
        3. 读取并解析 JSON
        4. 返回解析结果列表
        """
        candidates = []  # 候选路径列表
        seen = set()  # 已添加的路径集合（避免重复）

        def add_candidate_path(p: Path):
            """添加候选路径（如果未添加过）"""
            if p not in seen:
                seen.add(p)
                candidates.append(p)

        # 根据后端类型和解析方法，构建可能的输出路径
        # MinerU 的输出可能在不同的子目录中（vlm/method/auto）
        if backend.startswith("vlm-"):
            # VLM 后端优先查找 vlm 目录
            add_candidate_path(output_dir / file_stem / "vlm")
            if method:
                add_candidate_path(output_dir / file_stem / method)
            add_candidate_path(output_dir / file_stem / "auto")
        else:
            # 其他后端优先查找 method 目录
            if method:
                add_candidate_path(output_dir / file_stem / method)
            add_candidate_path(output_dir / file_stem / "vlm")
            add_candidate_path(output_dir / file_stem / "auto")

        # 在候选路径中查找 JSON 文件
        json_file = None
        subdir = None
        for sub in candidates:
            jf = sub / f"{file_stem}_content_list.json"  # JSON 文件名格式
            if jf.exists():
                subdir = sub
                json_file = jf
                break

        # 如果找不到 JSON 文件，抛出异常
        if not json_file:
            raise FileNotFoundError(f"[MinerU] Missing output file, tried: {', '.join(str(c / (file_stem + '_content_list.json')) for c in candidates)}")

        # 读取并解析 JSON 文件
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 将相对路径转换为绝对路径（用于图片、表格等资源文件）
        for item in data:
            for key in ("img_path", "table_img_path", "equation_img_path"):
                if key in item and item[key]:
                    # 将相对路径解析为绝对路径
                    item[key] = str((subdir / item[key]).resolve())
        return data

    def _process_image_path(self, image_path: str) -> str:
        """
        处理图像路径，上传到 Dify 并获取分析结果，返回处理后的字符串
        
        Args:
            image_path: 图像文件路径
            
        Returns:
            处理后的字符串
        """
        try:
            # 从环境变量获取 Dify 配置
            dify_host = os.getenv("DIFY_HOST", "http://192.168.50.222:8081")
            api_key = os.getenv("DIFY_API_KEY", "app-x907HXkO5tjarCsG49aNIrab")
            user_id = os.getenv("DIFY_USER_ID", "test_user_001")
            image_input_key = os.getenv("DIFY_IMAGE_INPUT_KEY", "upImage")
            
            if not api_key:
                self.logger.warning("[MinerU] DIFY_API_KEY not set, skipping image processing")
                return ""
            
            if not os.path.isfile(image_path):
                self.logger.warning(f"[MinerU] Image file not found: {image_path}")
                return ""
            
            # Step 1: Upload image
            upload_url = f"{dify_host}/v1/files/upload"
            upload_headers = {
                "Authorization": f"Bearer {api_key}"
            }
            
            with open(image_path, "rb") as f:
                files = {
                    "file": (os.path.basename(image_path), f, "image/jpeg")
                }
                data = {
                    "user": user_id
                }
                
                self.logger.info(f"[MinerU] Uploading image to Dify: {image_path}")
                r = requests.post(upload_url, headers=upload_headers, files=files, data=data, timeout=60)
            
            r.raise_for_status()
            upload_response = r.json()
            file_id = upload_response.get("id")
            
            if not file_id:
                self.logger.warning("[MinerU] Upload response missing file id")
                return ""
            
            self.logger.info(f"[MinerU] Image uploaded successfully, file_id: {file_id}")
            
            # Step 2: Run workflow
            run_url = f"{dify_host}/v1/workflows/run"
            run_headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "user": user_id,
                "response_mode": "blocking",
                "inputs": {
                    "query": "请分析这张图片",
                    image_input_key: {
                        "type": "image",
                        "transfer_method": "local_file",
                        "upload_file_id": file_id
                    }
                }
            }
            
            self.logger.info("[MinerU] Running Dify workflow")
            r = requests.post(run_url, headers=run_headers, json=payload, timeout=300)
            
            if r.status_code >= 400:
                self.logger.warning(f"[MinerU] Workflow request failed with status {r.status_code}: {r.text}")
                return ""
            
            r.raise_for_status()
            outer = r.json()
            
            # Step 3: Parse response
            data_obj = outer.get("data") if isinstance(outer, dict) else None
            if not isinstance(data_obj, dict):
                self.logger.warning("[MinerU] Response missing data object")
                return ""
            
            outputs = data_obj.get("outputs")
            if not isinstance(outputs, dict):
                self.logger.warning("[MinerU] Response missing outputs object")
                return ""
            
            result_value = outputs.get("result")
            
            # Parse result (may be JSON string or dict)
            if isinstance(result_value, str):
                try:
                    inner = json.loads(result_value)
                except json.JSONDecodeError:
                    self.logger.warning("[MinerU] Failed to parse result as JSON")
                    return ""
            elif isinstance(result_value, dict):
                inner = result_value
            else:
                inner = outputs
            
            # Extract and concatenate fields
            section_text = "\n".join([
                inner.get("summary", "")
            ]).strip()
            
            self.logger.info(f"[MinerU] Image processing completed, result length: {len(section_text)}")
            return section_text
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"[MinerU] Request error during image processing: {e}")
            return ""
        except Exception as e:
            self.logger.error(f"[MinerU] Error processing image: {e}", exc_info=True)
            return ""

    def _transfer_to_sections(self, outputs: list[dict[str, Any]], parse_method: str = None):
        """
        将 MinerU 的输出转换为 RAGFlow 的 sections 格式
    
        步骤：
        1. 遍历每个 output
        2. 根据 type 提取文本内容
        3. 生成位置标签
        4. 根据 parse_method 决定输出格式
        """
        sections = []
        for output in outputs:
            # 根据内容类型提取文本
            match output["type"]:
                case MinerUContentType.TEXT:
                    # 文本类型：直接使用文本内容
                    section = output["text"]
                case MinerUContentType.TABLE:
                    # 表格类型：组合表格主体、标题和脚注
                    section = output.get("table_body", "") + "\n".join(output.get("table_caption", [])) + "\n".join(output.get("table_footnote", []))
                    if not section.strip():
                        section = "FAILED TO PARSE TABLE"  # 如果解析失败，使用占位文本
                case MinerUContentType.IMAGE:
                    # self.logger.info(f"[MinerU==============>] Image: {output}")
                    # image_path = output.get("img_path", "")
                    # processed_result = ""
                    # self.logger.info(f"[MinerU==============>] image_path："+image_path)
                    # if image_path:
                    #     processed_result = self._process_image_path(image_path)
                    #     caption = "".join(output.get("image_caption", [])) or processed_result
                    #     section = caption + "\n" + "".join(output.get("image_footnote", []))
                    # else:
                        section = "".join(output.get("image_caption", [])) + "\n" + "".join(output.get("image_footnote", []))
                case MinerUContentType.EQUATION:
                    # 公式类型：使用文本内容
                    section = output["text"]
                case MinerUContentType.CODE:
                    # 代码类型：组合代码主体和标题
                    section = output["code_body"] + "\n".join(output.get("code_caption", []))
                case MinerUContentType.LIST:
                    # 列表类型：组合列表项
                    section = "\n".join(output.get("list_items", []))
                case MinerUContentType.DISCARDED:
                    # 丢弃的内容：跳过
                    pass

            # 根据解析方法决定输出格式
            if section and parse_method == "manual":
                # manual 模式：包含类型信息
                sections.append((section, output["type"], self._line_tag(output)))
            elif section and parse_method == "paper":
                # paper 模式：位置标签附加到文本末尾
                sections.append((section + self._line_tag(output), output["type"]))
            else:
                # 默认模式：文本和位置标签分开
                sections.append((section, self._line_tag(output)))
        return sections

    def _transfer_to_tables(self, outputs: list[dict[str, Any]]):
        return []

    def parse_pdf(
        self,
        filepath: str | PathLike[str],
        binary: BytesIO | bytes,
        callback: Optional[Callable] = None,
        *,
        output_dir: Optional[str] = None,
        backend: str = "pipeline",
        lang: Optional[str] = None,
        method: str = "auto",
        server_url: Optional[str] = None,
        delete_output: bool = True,
        parse_method: str = "raw",
    ) -> tuple:
        """
        解析 PDF 文件的主方法
        
        Args:
            filepath: PDF 文件路径
            binary: PDF 文件的二进制数据（如果提供了 filepath，可以为 None）
            callback: 进度回调函数
            output_dir: MinerU 输出目录（如果为 None，会创建临时目录）
            backend: MinerU 后端类型
            lang: 语言设置
            method: 解析方法（"auto"/"manual"/"paper"）
            server_url: VLM 服务器 URL
            delete_output: 是否在解析完成后删除输出目录
            parse_method: 解析方法（"raw"/"manual"/"paper"），影响输出格式
        
        Returns:
            tuple: (sections, tables) - sections 是文本块列表，tables 是表格列表（MinerU 目前返回空列表）
        """
        import shutil

        temp_pdf = None  # 临时 PDF 文件路径（如果从 binary 创建）
        created_tmp_dir = False  # 是否创建了临时目录

        # 移除文件名中的空格（MinerU 对文件名中的空格处理有问题）
        file_path = Path(filepath)
        pdf_file_name = file_path.stem.replace(" ", "") + ".pdf"
        pdf_file_path_valid = os.path.join(file_path.parent, pdf_file_name)

        if binary:
            temp_dir = Path(tempfile.mkdtemp(prefix="mineru_bin_pdf_"))
            temp_pdf = temp_dir / pdf_file_name
            with open(temp_pdf, "wb") as f:
                f.write(binary)
            pdf = temp_pdf
            self.logger.info(f"[MinerU] Received binary PDF -> {temp_pdf}")
            if callback:
                callback(0.15, f"[MinerU] Received binary PDF -> {temp_pdf}")
        else:
            if pdf_file_path_valid != filepath:
                self.logger.info(f"[MinerU] Remove all space in file name: {pdf_file_path_valid}")
                shutil.move(filepath, pdf_file_path_valid)
            pdf = Path(pdf_file_path_valid)
            if not pdf.exists():
                if callback:
                    callback(-1, f"[MinerU] PDF not found: {pdf}")
                raise FileNotFoundError(f"[MinerU] PDF not found: {pdf}")

        if output_dir:
            out_dir = Path(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        else:
            out_dir = Path(tempfile.mkdtemp(prefix="mineru_pdf_"))
            created_tmp_dir = True

        self.logger.info(f"[MinerU] Output directory: {out_dir}")
        if callback:
            callback(0.15, f"[MinerU] Output directory: {out_dir}")

        # 加载 PDF 页面图片（用于后续的图片裁剪）
        self.__images__(pdf, zoomin=1)

        try:
            # 运行 MinerU 解析 PDF
            self._run_mineru(pdf, out_dir, method=method, backend=backend, lang=lang, server_url=server_url, callback=callback)
            # 读取 MinerU 的输出 JSON 文件
            outputs = self._read_output(out_dir, pdf.stem, method=method, backend=backend)
            self.logger.info(f"[MinerU] Parsed {len(outputs)} blocks from PDF.")
            if callback:
                callback(0.75, f"[MinerU] Parsed {len(outputs)} blocks from PDF.")

            # 转换为 sections 格式并返回
            return self._transfer_to_sections(outputs, parse_method), self._transfer_to_tables(outputs)
        finally:
            # 清理临时文件
            if temp_pdf and temp_pdf.exists():
                try:
                    temp_pdf.unlink()  # 删除临时 PDF 文件
                    temp_pdf.parent.rmdir()  # 删除临时目录
                except Exception:
                    pass
            # 如果配置了删除输出，且创建了临时目录，则删除输出目录
            if delete_output and created_tmp_dir and out_dir.exists():
                try:
                    shutil.rmtree(out_dir)
                except Exception:
                    pass


if __name__ == "__main__":
    parser = MinerUParser("mineru")
    ok, reason = parser.check_installation()
    print("MinerU available:", ok)

    filepath = ""
    with open(filepath, "rb") as file:
        outputs = parser.parse_pdf(filepath=filepath, binary=file.read())
        for output in outputs:
            print(output)
