# MinerU 完整代码调用链详解

本文档详细解析 MinerU 解析出的 TEXT、TABLE、IMAGE 从 section 到 chunk 的完整转化过程，包括每个函数的代码、调用关系和数据流向。

---

## 一、整体调用链概览

```
MinerU 解析 PDF
    ↓
parse_pdf() → _read_output() → _transfer_to_sections()
    ↓
Section (文本, 位置标签)
    ↓
parser.py: _pdf() → pdf_parser.crop() → pdf_parser.extract_positions()
    ↓
Bbox {text, image, positions}
    ↓
parser.py: _invoke() → image2id()
    ↓
JSON {text, img_id, position_tag, positions}
    ↓
splitter.py: _invoke() → id2image() → naive_merge_with_images()
    ↓
naive_merge_with_images() → add_chunk() → concat_img()
    ↓
Chunk {text, image, positions}
```

---

## 二、阶段 1：MinerU 输出 → Section

### 2.1 调用入口

**文件：** `rag/flow/parser/parser.py`  
**方法：** `_pdf()` (MinerU 分支，第 272-302 行)

```python
# 创建 MinerUParser 实例
pdf_parser = MinerUParser(mineru_path=mineru_executable, mineru_api=mineru_api)

# 调用 parse_pdf 方法
lines, _ = pdf_parser.parse_pdf(
    filepath=name,
    binary=blob,
    callback=self.callback,
    output_dir=os.environ.get("MINERU_OUTPUT_DIR", ""),
    delete_output=bool(int(os.environ.get("MINERU_DELETE_OUTPUT", 1))),
)
```

**调用链：**
```
parser.py: _pdf()
    ↓
MinerUParser.parse_pdf()
    ↓
MinerUParser._run_mineru()  # 运行 MinerU 解析
    ↓
MinerUParser._read_output()  # 读取 JSON 输出
    ↓
MinerUParser._transfer_to_sections()  # 转换为 sections
```

---

### 2.2 MinerUParser.parse_pdf()

**文件：** `deepdoc/parser/mineru_parser.py`  
**方法：** `parse_pdf()` (第 860-960 行)

**完整代码流程：**

```python
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
    
    步骤：
    1. 准备输出目录
    2. 保存 PDF 文件到临时目录
    3. 运行 MinerU 解析
    4. 读取 MinerU 的输出 JSON
    5. 转换为 sections 格式
    6. 清理临时文件
    """
    # 步骤 1: 准备输出目录
    if output_dir:
        out_dir = Path(output_dir)
        created_tmp_dir = False
    else:
        # 创建临时目录
        out_dir = Path(tempfile.mkdtemp(prefix="mineru_output_"))
        created_tmp_dir = True
    
    # 步骤 2: 保存 PDF 文件
    temp_pdf = None
    if binary:
        # 如果提供了二进制数据，保存到临时文件
        temp_dir = Path(tempfile.mkdtemp(prefix="mineru_pdf_"))
        temp_pdf = temp_dir / Path(filepath).name
        with open(temp_pdf, "wb") as f:
            f.write(binary if isinstance(binary, bytes) else binary.read())
        filepath = str(temp_pdf)
    
    # 步骤 3: 加载 PDF 页面图片（用于后续的图片裁剪）
    pdf = fitz.open(filepath)  # 使用 PyMuPDF 打开 PDF
    self.__images__(pdf, zoomin=1)  # 加载所有页面的图片
    
    try:
        # 步骤 4: 运行 MinerU 解析 PDF
        self._run_mineru(pdf, out_dir, method=method, backend=backend, lang=lang, server_url=server_url, callback=callback)
        
        # 步骤 5: 读取 MinerU 的输出 JSON 文件
        outputs = self._read_output(out_dir, pdf.stem, method=method, backend=backend)
        
        # 步骤 6: 转换为 sections 格式并返回
        return self._transfer_to_sections(outputs, parse_method), self._transfer_to_tables(outputs)
    finally:
        # 步骤 7: 清理临时文件
        if temp_pdf and temp_pdf.exists():
            temp_pdf.unlink()
            temp_pdf.parent.rmdir()
        if delete_output and created_tmp_dir and out_dir.exists():
            shutil.rmtree(out_dir)
```

**关键调用：**
- `self.__images__(pdf, zoomin=1)` → 加载 PDF 页面图片到 `self.page_images`
- `self._run_mineru()` → 运行 MinerU 解析
- `self._read_output()` → 读取 JSON 输出
- `self._transfer_to_sections()` → 转换为 sections

---

### 2.3 MinerUParser._read_output()

**文件：** `deepdoc/parser/mineru_parser.py`  
**方法：** `_read_output()` (第 611-680 行)

**功能：** 读取 MinerU 生成的 JSON 文件（`{file_stem}_content_list.json`）

**代码流程：**

```python
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
    
    # 步骤 1: 根据后端类型和解析方法，构建可能的输出路径
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
    
    # 步骤 2: 在候选路径中查找 JSON 文件
    json_file = None
    for candidate_dir in candidates:
        json_path = candidate_dir / f"{file_stem}_content_list.json"
        if json_path.exists():
            json_file = json_path
            break
    
    if not json_file:
        raise FileNotFoundError(f"[MinerU] Missing output file: {file_stem}_content_list.json")
    
    # 步骤 3: 读取并解析 JSON
    with open(json_file, "r", encoding="utf-8") as f:
        outputs = json.load(f)
    
    # 步骤 4: 返回解析结果列表
    return outputs  # list[dict]，每个元素是一个内容块
```

**返回数据格式：**
```python
outputs = [
    {
        "type": "text",  # 或 "table", "image", "equation", "code", "list"
        "text": "文本内容",  # TEXT 类型
        "bbox": [x0, top, x1, bottom],  # 边界框（相对坐标 0-1000）
        "page_idx": 0,  # 页码（从 0 开始）
        # ... 其他字段
    },
    # ... 更多内容块
]
```

---

### 2.4 MinerUParser._transfer_to_sections()

**文件：** `deepdoc/parser/mineru_parser.py`  
**方法：** `_transfer_to_sections()` (第 797-855 行)

**功能：** 将 MinerU 的输出转换为 RAGFlow 的 sections 格式

**完整代码解析：**

```python
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
        section = None  # 初始化 section 文本
        
        # 步骤 1: 根据内容类型提取文本
        match output["type"]:
            case MinerUContentType.TEXT:
                # TEXT 类型：直接使用文本内容
                section = output["text"]
                
            case MinerUContentType.TABLE:
                # TABLE 类型：组合表格主体、标题和脚注
                table_body = output.get("table_body", "")
                table_caption = "\n".join(output.get("table_caption", []))
                table_footnote = "\n".join(output.get("table_footnote", []))
                section = table_body + table_caption + table_footnote
                if not section.strip():
                    section = "FAILED TO PARSE TABLE"  # 如果解析失败，使用占位文本
                    
            case MinerUContentType.IMAGE:
                # IMAGE 类型：组合图片标题和脚注
                image_caption = "".join(output.get("image_caption", []))
                image_footnote = "".join(output.get("image_footnote", []))
                section = image_caption + "\n" + image_footnote
                
            case MinerUContentType.EQUATION:
                # EQUATION 类型：使用文本内容
                section = output["text"]
                
            case MinerUContentType.CODE:
                # CODE 类型：组合代码主体和标题
                code_body = output.get("code_body", "")
                code_caption = "\n".join(output.get("code_caption", []))
                section = code_body + code_caption
                
            case MinerUContentType.LIST:
                # LIST 类型：组合列表项
                section = "\n".join(output.get("list_items", []))
                
            case MinerUContentType.DISCARDED:
                # DISCARDED 类型：跳过
                continue
        
        # 步骤 2: 生成位置标签
        # 调用 _line_tag() 方法生成位置标签字符串
        position_tag = self._line_tag(output)
        
        # 步骤 3: 根据解析方法决定输出格式
        if section and parse_method == "manual":
            # manual 模式：包含类型信息
            sections.append((section, output["type"], position_tag))
        elif section and parse_method == "paper":
            # paper 模式：位置标签附加到文本末尾
            sections.append((section + position_tag, output["type"]))
        else:
            # 默认模式：文本和位置标签分开
            sections.append((section, position_tag))
    
    return sections
```

**关键调用：**
- `self._line_tag(output)` → 生成位置标签字符串

---

### 2.5 MinerUParser._line_tag()

**文件：** `deepdoc/parser/mineru_parser.py`  
**方法：** `_line_tag()` (第 399-424 行)

**功能：** 生成位置标签字符串（格式：`@@页码\tx0\tx1\ttop\tbottom##`）

**完整代码解析：**

```python
def _line_tag(self, bx):
    """
    生成位置标签字符串
    
    步骤：
    1. 获取页码（从 page_idx 转换，从 0 开始 → 从 1 开始）
    2. 获取边界框坐标（bbox）
    3. 如果已加载页面图片，将相对坐标（0-1000）转换为绝对像素坐标
    4. 生成位置标签字符串
    """
    # 步骤 1: 获取页码（从 1 开始）
    pn = [bx["page_idx"] + 1]
    
    # 步骤 2: 获取边界框坐标
    positions = bx.get("bbox", (0, 0, 0, 0))  # (x0, top, x1, bottom)
    x0, top, x1, bott = positions
    
    # 步骤 3: 如果已加载页面图片，将相对坐标转换为绝对像素坐标
    if hasattr(self, "page_images") and self.page_images and len(self.page_images) > bx["page_idx"]:
        # 获取页面尺寸
        page_width, page_height = self.page_images[bx["page_idx"]].size
        
        # MinerU 使用 0-1000 的相对坐标，需要转换为像素坐标
        x0 = (x0 / 1000.0) * page_width
        x1 = (x1 / 1000.0) * page_width
        top = (top / 1000.0) * page_height
        bott = (bott / 1000.0) * page_height
    
    # 步骤 4: 生成位置标签字符串
    # 格式：@@页码\tx0\tx1\ttop\tbottom##
    return "@@{}\t{:.1f}\t{:.1f}\t{:.1f}\t{:.1f}##".format(
        "-".join([str(p) for p in pn]),  # 页码（支持范围，如 "1-3"）
        x0, x1, top, bott
    )
```

**返回示例：**
```python
"@@1\t143.0\t540.0\t154.0\t279.0##"
```

**数据流向：**
```
output["page_idx"] = 0
    ↓
pn = [1]  # 从 0 开始 → 从 1 开始
    ↓
output["bbox"] = [143.0, 154.0, 540.0, 279.0]  # 相对坐标（0-1000）
    ↓
转换为像素坐标（如果已加载页面图片）
    ↓
"@@1\t143.0\t540.0\t154.0\t279.0##"
```

---

## 三、阶段 2：Section → Bbox

### 3.1 调用入口

**文件：** `rag/flow/parser/parser.py`  
**方法：** `_pdf()` (MinerU 分支，第 294-302 行)

**完整代码解析：**

```python
# 步骤 1: 调用 MinerUParser.parse_pdf() 获取 sections
lines, _ = pdf_parser.parse_pdf(...)
# lines = [(文本内容, 位置标签), ...]

# 步骤 2: 将 sections 转换为 bboxes
bboxes = []
for t, poss in lines:  # t: 文本内容, poss: 位置标签字符串
    # 步骤 2.1: 调用 crop() 方法裁剪图片
    image = pdf_parser.crop(poss, 1)
    # 调用链：crop() → extract_positions() → 从 page_images 裁剪
    
    # 步骤 2.2: 调用 extract_positions() 提取位置信息
    positions = pdf_parser.extract_positions(poss)
    # positions = [([页码], x0, x1, top, bottom), ...]
    
    # 步骤 2.3: 构建 bbox 字典
    box = {
        "image": image,  # PIL.Image 对象或 None
        "positions": [[pos[0][-1], *pos[1:]] for pos in positions],
        # positions 格式转换：[页码, x0, x1, top, bottom]
        "text": t,  # 文本内容
    }
    bboxes.append(box)
```

**关键调用：**
- `pdf_parser.crop(poss, 1)` → 裁剪图片
- `pdf_parser.extract_positions(poss)` → 提取位置信息

---

### 3.2 MinerUParser.extract_positions()

**文件：** `deepdoc/parser/mineru_parser.py`  
**方法：** `extract_positions()` (第 586-609 行)

**功能：** 从位置标签字符串中提取位置信息

**完整代码解析：**

```python
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
    
    # 步骤 1: 使用正则表达式查找所有位置标签
    # 模式：@@页码\tx0\tx1\ttop\tbottom##
    for tag in re.findall(r"@@[0-9-]+\t[0-9.\t]+##", txt):
        # 步骤 2: 解析标签
        # 去除 @@ 和 ##，按制表符分割
        pn, left, right, top, bottom = tag.strip("#").strip("@").split("\t")
        
        # 步骤 3: 转换为浮点数
        left, right, top, bottom = float(left), float(right), float(top), float(bottom)
        
        # 步骤 4: 解析页码（支持范围，如 "1-3"）
        # 页码从 1 开始，转换为从 0 开始的索引
        pn_list = [int(p) - 1 for p in pn.split("-")]
        
        # 步骤 5: 添加到结果列表
        poss.append((pn_list, left, right, top, bottom))
    
    return poss
```

**输入示例：**
```python
txt = "@@1\t143.0\t540.0\t154.0\t279.0##"
```

**处理流程：**
```
正则匹配：@@1\t143.0\t540.0\t154.0\t279.0##
    ↓
去除 @@ 和 ##：1\t143.0\t540.0\t154.0\t279.0
    ↓
按制表符分割：["1", "143.0", "540.0", "154.0", "279.0"]
    ↓
转换为浮点数：[1, 143.0, 540.0, 154.0, 279.0]
    ↓
解析页码：pn_list = [0]  # 1 - 1 = 0
    ↓
返回：([0], 143.0, 540.0, 154.0, 279.0)
```

**返回格式：**
```python
[
    ([页码列表], x0, x1, top, bottom),
    # 例如：([0], 143.0, 540.0, 154.0, 279.0)
]
```

---

### 3.3 MinerUParser.crop()

**文件：** `deepdoc/parser/mineru_parser.py`  
**方法：** `crop()` (第 426-580 行)

**功能：** 根据位置标签从 PDF 页面中裁剪图片

**完整代码解析：**

```python
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
    
    # 步骤 1: 从文本中提取位置信息
    poss = self.extract_positions(text)
    if not poss:
        # 如果没有位置信息，返回 None
        if need_position:
            return None, None
        return
    
    # 步骤 2: 检查是否已加载页面图片
    if not getattr(self, "page_images", None):
        self.logger.warning("[MinerU] crop called without page images; skipping image generation.")
        if need_position:
            return None, None
        return
    
    page_count = len(self.page_images)  # PDF 总页数
    
    # 步骤 3: 过滤位置信息（移除无效的页码索引）
    filtered_poss = []
    for pns, left, right, top, bottom in poss:
        if not pns:
            continue
        # 只保留有效的页码索引（在页面范围内）
        valid_pns = [p for p in pns if 0 <= p < page_count]
        if not valid_pns:
            continue
        filtered_poss.append((valid_pns, left, right, top, bottom))
    
    poss = filtered_poss
    if not poss:
        if need_position:
            return None, None
        return
    
    # 步骤 4: 计算最大宽度（用于统一裁剪宽度）
    max_width = max(np.max([right - left for (_, left, right, _, _) in poss]), 6)
    GAP = 6  # 图片片段之间的间距（像素）
    
    # 步骤 5: 在第一个位置之前添加扩展区域（向上扩展 120 像素）
    pos = poss[0]
    first_page_idx = pos[0][0]
    poss.insert(0, ([first_page_idx], pos[1], pos[2], max(0, pos[3] - 120), max(pos[3] - GAP, 0)))
    
    # 步骤 6: 在最后一个位置之后添加扩展区域（向下扩展 120 像素）
    pos = poss[-1]
    last_page_idx = pos[0][-1]
    if not (0 <= last_page_idx < page_count):
        if need_position:
            return None, None
        return
    last_page_height = self.page_images[last_page_idx].size[1]
    poss.append(
        ([last_page_idx], pos[1], pos[2], min(last_page_height, pos[4] + GAP), min(last_page_height, pos[4] + 120))
    )
    
    # 步骤 7: 裁剪每个位置的图片片段
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
        
        # 裁剪第一页的图片片段
        if not (0 <= pns[0] < page_count):
            continue
        
        img0 = self.page_images[pns[0]]  # 获取页面图片
        x0, y0, x1, y1 = int(left), int(top), int(right), int(min(bottom, img0.size[1]))
        crop0 = img0.crop((x0, y0, x1, y1))  # 使用 PIL.Image.crop() 裁剪
        imgs.append(crop0)
        
        # 记录位置信息（不包括首尾的扩展区域）
        if 0 < ii < len(poss) - 1:
            positions.append((pns[0] + self.page_from, x0, x1, y0, y1))
        
        # 如果跨页，需要裁剪后续页面的图片
        bottom -= img0.size[1]
        for pn in pns[1:]:
            if not (0 <= pn < page_count):
                continue
            page = self.page_images[pn]
            x0, y0, x1, y1 = int(left), 0, int(right), int(min(bottom, page.size[1]))
            cimgp = page.crop((x0, y0, x1, y1))
            imgs.append(cimgp)
            if 0 < ii < len(poss) - 1:
                positions.append((pn + self.page_from, x0, x1, y0, y1))
            bottom -= page.size[1]
    
    # 步骤 8: 如果没有裁剪到任何图片，返回 None
    if not imgs:
        if need_position:
            return None, None
        return
    
    # 步骤 9: 计算合并后图片的总高度和宽度
    height = 0
    for img in imgs:
        height += img.size[1] + GAP
    height = int(height)
    width = int(np.max([i.size[0] for i in imgs]))
    
    # 步骤 10: 创建新的空白图片（浅灰色背景）
    pic = Image.new("RGB", (width, height), (245, 245, 245))
    height = 0
    
    # 步骤 11: 将所有图片片段垂直拼接
    for ii, img in enumerate(imgs):
        # 对首尾的扩展区域添加半透明遮罩
        if ii == 0 or ii + 1 == len(imgs):
            img = img.convert("RGBA")
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            overlay.putalpha(128)  # 50% 透明度
            img = Image.alpha_composite(img, overlay).convert("RGB")
        
        # 将图片片段粘贴到合并图片上
        pic.paste(img, (0, int(height)))
        height += img.size[1] + GAP
    
    # 步骤 12: 返回结果
    if need_position:
        return pic, positions
    return pic
```

**关键调用：**
- `self.extract_positions(text)` → 提取位置信息
- `self.page_images[pn].crop((x0, y0, x1, y1))` → PIL.Image.crop() 裁剪图片
- `Image.new("RGB", (width, height), (245, 245, 245))` → 创建新图片
- `pic.paste(img, (0, int(height)))` → 粘贴图片片段

**数据流向：**
```
text = "@@1\t143.0\t540.0\t154.0\t279.0##"
    ↓
extract_positions() → [([0], 143.0, 540.0, 154.0, 279.0)]
    ↓
从 page_images[0] 裁剪区域 (143, 154, 540, 279)
    ↓
crop0 = PIL.Image 对象
    ↓
垂直拼接（如果有多个片段）
    ↓
pic = PIL.Image 对象（合并后的图片）
```

---

## 四、阶段 3：Bbox → JSON（图片上传）

### 4.1 调用入口

**文件：** `rag/flow/parser/parser.py`  
**方法：** `_invoke()` (第 882-887 行)

**完整代码解析：**

```python
# 获取解析结果
outs = self.output()
# outs = {"json": [bbox1, bbox2, ...], ...}

# 异步处理所有 JSON 输出中的图片
async with trio.open_nursery() as nursery:
    for d in outs.get("json", []):  # 遍历每个 bbox
        # 调用 image2id 上传图片并替换为 img_id
        nursery.start_soon(
            image2id,  # 上传函数
            d,  # bbox 字典
            partial(settings.STORAGE_IMPL.put, tenant_id=self._canvas._tenant_id),  # 上传函数
            get_uuid()  # 生成唯一 ID
        )
```

**关键调用：**
- `image2id()` → 上传图片并替换为 img_id

---

### 4.2 image2id()

**文件：** `rag/utils/base64_image.py`  
**函数：** `image2id()` (第 28-59 行)

**功能：** 将 PIL.Image 对象上传到对象存储，替换为 img_id

**完整代码解析：**

```python
async def image2id(d: dict, storage_put_func: partial, objname: str, bucket: str = "imagetemps"):
    """
    将图片对象转换为存储 ID
    
    步骤：
    1. 检查是否有图片
    2. 将 PIL.Image 转换为 JPEG 字节流
    3. 上传到对象存储
    4. 生成 img_id 并替换
    """
    # 步骤 1: 检查是否有图片
    if "image" not in d:
        return
    if not d["image"]:
        del d["image"]
        return
    
    # 步骤 2: 将 PIL.Image 转换为 JPEG 字节流
    with BytesIO() as output_buffer:
        if isinstance(d["image"], bytes):
            # 如果已经是字节流，直接写入
            output_buffer.write(d["image"])
            output_buffer.seek(0)
        else:
            # 如果是 PIL.Image 对象
            # 如果图片是 RGBA 或 P 模式，转换为 RGB 模式
            if d["image"].mode in ("RGBA", "P"):
                converted_image = d["image"].convert("RGB")
                d["image"] = converted_image
            
            # 保存为 JPEG 格式
            try:
                d["image"].save(output_buffer, format='JPEG')
            except OSError as e:
                logging.warning("Saving image exception, ignore: {}".format(str(e)))
        
        # 步骤 3: 上传到对象存储
        async with minio_limiter:  # 限制并发数
            await trio.to_thread.run_sync(
                lambda: storage_put_func(bucket=bucket, fnm=objname, binary=output_buffer.getvalue())
            )
        
        # 步骤 4: 生成 img_id 并替换
        d["img_id"] = f"{bucket}-{objname}"  # 格式：bucket-filename
        
        # 步骤 5: 清理图片对象
        if not isinstance(d["image"], bytes):
            d["image"].close()
        del d["image"]  # 删除图片引用
```

**关键调用：**
- `d["image"].save(output_buffer, format='JPEG')` → PIL.Image.save() 保存为 JPEG
- `storage_put_func(bucket=bucket, fnm=objname, binary=...)` → 上传到对象存储（MinIO/S3）

**数据流向：**
```
bbox = {"image": PIL.Image, "text": "...", ...}
    ↓
PIL.Image → JPEG 字节流
    ↓
上传到对象存储（bucket="imagetemps", filename="uuid-xxx"）
    ↓
生成 img_id = "imagetemps-uuid-xxx"
    ↓
bbox = {"img_id": "imagetemps-uuid-xxx", "text": "...", ...}
```

---

## 五、阶段 4：JSON → Section（Splitter 提取）

### 5.1 调用入口

**文件：** `rag/flow/splitter/splitter.py`  
**方法：** `_invoke()` (第 111-115 行)

**完整代码解析：**

```python
# 步骤 1: 从 JSON 输出中提取 sections 和 images
sections, section_images = [], []
for o in from_upstream.json_result or []:  # 遍历每个 bbox
    # 步骤 1.1: 提取文本和位置标签
    sections.append((
        o.get("text", ""),           # 文本内容
        o.get("position_tag", "")    # 位置标签字符串
    ))
    
    # 步骤 1.2: 从对象存储获取图片
    section_images.append(
        id2image(  # 从对象存储下载图片，转换为 PIL.Image
            o.get("img_id"),  # 图片 ID
            partial(settings.STORAGE_IMPL.get, tenant_id=self._canvas._tenant_id)  # 下载函数
        )
    )

# 步骤 2: 调用 naive_merge_with_images 合并成 chunks
chunks, images = naive_merge_with_images(
    sections,
    section_images,
    self._param.chunk_token_size,
    deli,
    self._param.overlapped_percent,
)
```

**关键调用：**
- `id2image()` → 从对象存储下载图片
- `naive_merge_with_images()` → 合并成 chunks

---

### 5.2 id2image()

**文件：** `rag/utils/base64_image.py`  
**函数：** `id2image()` (第 62-76 行)

**功能：** 从对象存储下载图片，转换为 PIL.Image 对象

**完整代码解析：**

```python
def id2image(image_id: str | None, storage_get_func: partial):
    """
    从对象存储下载图片，转换为 PIL.Image 对象
    
    步骤：
    1. 解析 img_id（格式：bucket-filename）
    2. 从对象存储下载图片
    3. 转换为 PIL.Image 对象
    """
    # 步骤 1: 检查 img_id 是否存在
    if not image_id:
        return None
    
    # 步骤 2: 解析 img_id（格式：bucket-filename）
    arr = image_id.split("-")
    if len(arr) != 2:
        return None
    bkt, nm = image_id.split("-")  # bucket, filename
    
    # 步骤 3: 从对象存储下载图片
    try:
        blob = storage_get_func(bucket=bkt, filename=nm)
        if not blob:
            return None
        
        # 步骤 4: 转换为 PIL.Image 对象
        return Image.open(BytesIO(blob))
    except Exception as e:
        logging.exception(e)
        return None
```

**关键调用：**
- `storage_get_func(bucket=bkt, filename=nm)` → 从对象存储下载
- `Image.open(BytesIO(blob))` → PIL.Image.open() 打开图片

**数据流向：**
```
img_id = "imagetemps-uuid-xxx"
    ↓
解析：bucket="imagetemps", filename="uuid-xxx"
    ↓
从对象存储下载：blob = bytes
    ↓
转换为 PIL.Image：Image.open(BytesIO(blob))
    ↓
返回：PIL.Image 对象
```

---

## 六、阶段 5：Section → Chunk（合并）

### 6.1 调用入口

**文件：** `rag/nlp/__init__.py`  
**函数：** `naive_merge_with_images()` (第 922-1090 行)

**功能：** 将多个 sections 合并成 chunks

**完整代码解析：**

```python
def naive_merge_with_images(texts, images, chunk_token_num=128, delimiter="\n。；！？", overlapped_percent=0):
    """
    将多个 sections（文本+图片）合并成 chunks
    
    步骤：
    1. 初始化 chunks 列表
    2. 遍历所有 sections
    3. 调用 add_chunk() 添加到 chunk
    """
    from deepdoc.parser.pdf_parser import RAGFlowPdfParser
    
    # 步骤 1: 参数验证
    if not texts or len(texts) != len(images):
        return [], []
    
    # 步骤 2: 初始化
    cks = [""]  # 当前只有一个空的 chunk
    result_images = [None]  # 对应的图片列表
    tk_nums = [0]  # 每个 chunk 的 token 数
    
    # 步骤 3: 定义内部函数 add_chunk()
    def add_chunk(t, image, pos=""):
        """核心分块函数：将文本和图片添加到 chunk 中"""
        nonlocal cks, result_images, tk_nums, delimiter
        
        # 计算文本的 token 数
        tnum = num_tokens_from_string(t)
        
        # 处理位置标签
        if not pos:
            pos = ""
        if tnum < 8:
            pos = ""
        
        # 判断是否需要创建新 chunk
        if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent)/100.:
            # 创建新 chunk
            if cks:
                overlapped = RAGFlowPdfParser.remove_tag(cks[-1])
                overlap_start = int(len(overlapped) * (100 - overlapped_percent) / 100.)
                t = overlapped[overlap_start:] + t
            
            if t.find(pos) < 0:
                t += pos
            
            cks.append(t)
            result_images.append(image)
            tk_nums.append(tnum)
        else:
            # 合并到当前 chunk
            if cks[-1].find(pos) < 0:
                t += pos
            
            cks[-1] += t  # 文本追加
            
            # 图片处理
            if result_images[-1] is None:
                result_images[-1] = image
            else:
                result_images[-1] = concat_img(result_images[-1], image)  # 垂直拼接
            
            tk_nums[-1] += tnum
    
    # 步骤 4: 处理自定义分隔符（如果有）
    custom_delimiters = [m.group(1) for m in re.finditer(r"`([^`]+)`", delimiter)]
    has_custom = bool(custom_delimiters)
    
    if has_custom:
        # 按自定义分隔符强制分割
        custom_pattern = "|".join(re.escape(t) for t in sorted(set(custom_delimiters), key=len, reverse=True))
        cks, result_images, tk_nums = [], [], []
        for text, image in zip(texts, images):
            text_str = text[0] if isinstance(text, tuple) else text
            text_pos = text[1] if isinstance(text, tuple) and len(text) > 1 else ""
            split_sec = re.split(r"(%s)" % custom_pattern, text_str)
            for sub_sec in split_sec:
                if re.fullmatch(custom_pattern, sub_sec or ""):
                    continue
                text_seg = "\n" + sub_sec
                local_pos = text_pos
                if num_tokens_from_string(text_seg) < 8:
                    local_pos = ""
                if local_pos and text_seg.find(local_pos) < 0:
                    text_seg += local_pos
                cks.append(text_seg)
                result_images.append(image)
                tk_nums.append(num_tokens_from_string(text_seg))
        return cks, result_images
    
    # 步骤 5: 正常合并流程（无自定义分隔符）
    for text, image in zip(texts, images):
        if isinstance(text, tuple):
            text_str = text[0]
            text_pos = text[1] if len(text) > 1 else ""
            add_chunk("\n" + text_str, image, text_pos)
        else:
            add_chunk("\n" + text, image)
    
    return cks, result_images
```

**关键调用：**
- `add_chunk()` → 内部函数，添加文本和图片到 chunk
- `num_tokens_from_string()` → 计算 token 数
- `concat_img()` → 垂直拼接图片
- `RAGFlowPdfParser.remove_tag()` → 移除位置标签

---

### 6.2 add_chunk() 内部函数

**位置：** `rag/nlp/__init__.py:958-1029`（在 `naive_merge_with_images()` 内部）

**功能：** 核心分块函数，将文本和图片添加到 chunk 中

**完整代码解析：**

```python
def add_chunk(t, image, pos=""):
    """
    核心分块函数：将文本和图片添加到 chunk 中
    
    步骤：
    1. 计算文本的 token 数
    2. 判断是否需要创建新 chunk
    3. 创建新 chunk 或合并到当前 chunk
    """
    nonlocal cks, result_images, tk_nums, delimiter
    
    # 步骤 1: 计算文本的 token 数
    tnum = num_tokens_from_string(t)
    
    # 步骤 2: 处理位置标签
    if not pos:
        pos = ""
    if tnum < 8:  # 如果文本太短，不添加位置标签
        pos = ""
    
    # 步骤 3: 判断是否需要创建新 chunk
    # 判断条件：当前 chunk 为空，或 token 数超过限制
    limit = chunk_token_num * (100 - overlapped_percent) / 100.
    if cks[-1] == "" or tk_nums[-1] > limit:
        # ========== 创建新 chunk ==========
        # 步骤 3.1: 如果有重叠，从上一个 chunk 的末尾提取重叠部分
        if cks:
            overlapped = RAGFlowPdfParser.remove_tag(cks[-1])
            overlap_start = int(len(overlapped) * (100 - overlapped_percent) / 100.)
            t = overlapped[overlap_start:] + t
        
        # 步骤 3.2: 添加位置标签（如果还没有）
        if t.find(pos) < 0:
            t += pos
        
        # 步骤 3.3: 创建新 chunk
        cks.append(t)
        result_images.append(image)
        tk_nums.append(tnum)
    else:
        # ========== 合并到当前 chunk ==========
        # 步骤 3.4: 添加位置标签（如果还没有）
        if cks[-1].find(pos) < 0:
            t += pos
        
        # 步骤 3.5: 文本追加到当前 chunk
        cks[-1] += t
        
        # 步骤 3.6: 图片处理
        if result_images[-1] is None:
            # 如果当前 chunk 没有图片，直接赋值
            result_images[-1] = image
        else:
            # 如果当前 chunk 已有图片，垂直拼接
            result_images[-1] = concat_img(result_images[-1], image)
        
        # 步骤 3.7: 更新 token 数
        tk_nums[-1] += tnum
```

**关键调用：**
- `num_tokens_from_string(t)` → 计算 token 数
- `RAGFlowPdfParser.remove_tag(cks[-1])` → 移除位置标签
- `concat_img(result_images[-1], image)` → 垂直拼接图片

---

### 6.3 concat_img()

**文件：** `rag/nlp/__init__.py`  
**函数：** `concat_img()` (第 1106-1132 行)

**功能：** 垂直拼接两个 PIL.Image 对象

**完整代码解析：**

```python
def concat_img(img1, img2):
    """
    垂直拼接两个 PIL.Image 对象
    
    步骤：
    1. 检查输入参数（处理 None 情况）
    2. 检查是否为同一图片对象
    3. 检查像素数据是否相同（避免重复拼接）
    4. 计算合并后的尺寸
    5. 创建新图片
    6. 粘贴两个图片
    """
    # 步骤 1: 检查输入参数（处理 None 情况）
    if img1 and not img2:
        return img1
    if not img1 and img2:
        return img2
    if not img1 and not img2:
        return None
    
    # 步骤 2: 检查是否为同一图片对象
    if img1 is img2:
        return img1
    
    # 步骤 3: 检查像素数据是否相同（避免重复拼接）
    if isinstance(img1, Image.Image) and isinstance(img2, Image.Image):
        pixel_data1 = img1.tobytes()
        pixel_data2 = img2.tobytes()
        if pixel_data1 == pixel_data2:
            return img1  # 如果像素数据相同，返回第一个图片
    
    # 步骤 4: 计算合并后的尺寸
    width1, height1 = img1.size
    width2, height2 = img2.size
    new_width = max(width1, width2)  # 最大宽度
    new_height = height1 + height2  # 总高度
    
    # 步骤 5: 创建新图片
    new_image = Image.new('RGB', (new_width, new_height))
    
    # 步骤 6: 粘贴两个图片
    new_image.paste(img1, (0, 0))  # 第一个图片在顶部
    new_image.paste(img2, (0, height1))  # 第二个图片在第一个图片下方
    
    # 步骤 7: 返回合并后的图片
    return new_image
```

**关键调用：**
- `Image.new("RGB", (width, height), (245, 245, 245))` → 创建新图片
- `pic.paste(img1, (0, 0))` → 粘贴第一个图片
- `pic.paste(img2, (0, img1.size[1]))` → 粘贴第二个图片

**数据流向：**
```
img1 = PIL.Image (width1, height1)
img2 = PIL.Image (width2, height2)
    ↓
width = max(width1, width2)
height = height1 + height2
    ↓
pic = Image.new("RGB", (width, height), (245, 245, 245))
    ↓
pic.paste(img1, (0, 0))
pic.paste(img2, (0, height1))
    ↓
返回：pic (合并后的图片)
```

---

## 七、完整调用链总结

### 7.1 TEXT 类型调用链

```
parser.py: _pdf()
    ↓
MinerUParser.parse_pdf()
    ↓
MinerUParser._read_output() → 读取 JSON
    ↓
MinerUParser._transfer_to_sections()
    ├─ case TEXT: section = output["text"]
    └─ MinerUParser._line_tag() → 生成位置标签
    ↓
Section: ("文本内容", "@@1\t143.0\t540.0\t154.0\t279.0##")
    ↓
parser.py: _pdf()
    ├─ MinerUParser.crop() → None（TEXT 类型通常没有图片）
    └─ MinerUParser.extract_positions() → 提取位置信息
    ↓
Bbox: {text, image=None, positions}
    ↓
parser.py: _invoke()
    └─ image2id() → 跳过（image=None）
    ↓
JSON: {text, img_id=None, position_tag, positions}
    ↓
splitter.py: _invoke()
    ├─ id2image(None) → None
    └─ naive_merge_with_images()
        └─ add_chunk() → 合并文本
    ↓
Chunk: {text, image=None, positions}
```

### 7.2 TABLE 类型调用链

```
parser.py: _pdf()
    ↓
MinerUParser.parse_pdf()
    ↓
MinerUParser._transfer_to_sections()
    ├─ case TABLE: section = table_body + table_caption + table_footnote
    └─ MinerUParser._line_tag() → 生成位置标签
    ↓
Section: ("表格内容", "@@2\t100.0\t500.0\t200.0\t400.0##")
    ↓
parser.py: _pdf()
    ├─ MinerUParser.crop() → None
    └─ MinerUParser.extract_positions() → 提取位置信息
    ↓
Bbox: {text, image=None, positions, doc_type_kwd="table"}
    ↓
parser.py: _invoke()
    └─ image2id() → 跳过
    ↓
JSON: {text, img_id=None, position_tag, positions, doc_type_kwd="table"}
    ↓
splitter.py: _invoke()
    ├─ id2image(None) → None
    └─ naive_merge_with_images()
        └─ add_chunk() → 合并文本
    ↓
Chunk: {text, image=None, positions}
```

### 7.3 IMAGE 类型调用链

```
parser.py: _pdf()
    ↓
MinerUParser.parse_pdf()
    ↓
MinerUParser._transfer_to_sections()
    ├─ case IMAGE: section = image_caption + image_footnote
    └─ MinerUParser._line_tag() → 生成位置标签
    ↓
Section: ("图片标题\n图片脚注", "@@4\t792.0\t967.0\t192.0\t342.0##")
    ↓
parser.py: _pdf()
    ├─ MinerUParser.crop()
    │   ├─ MinerUParser.extract_positions() → 提取位置信息
    │   ├─ self.page_images[pn].crop() → 裁剪图片
    │   └─ Image.new() + pic.paste() → 垂直拼接
    └─ MinerUParser.extract_positions() → 提取位置信息
    ↓
Bbox: {text, image=PIL.Image, positions, doc_type_kwd="image"}
    ↓
parser.py: _invoke()
    └─ image2id()
        ├─ d["image"].save() → 转换为 JPEG
        └─ storage_put_func() → 上传到对象存储
    ↓
JSON: {text, img_id="imagetemps-uuid-xxx", position_tag, positions, doc_type_kwd="image"}
    ↓
splitter.py: _invoke()
    ├─ id2image()
    │   ├─ storage_get_func() → 从对象存储下载
    │   └─ Image.open() → 转换为 PIL.Image
    └─ naive_merge_with_images()
        └─ add_chunk()
            └─ concat_img() → 垂直拼接图片（如果需要）
    ↓
Chunk: {text, image=PIL.Image, positions}
```

---

## 八、关键函数位置总结

| 函数 | 文件 | 行数 | 功能 |
|------|------|------|------|
| `parse_pdf()` | `deepdoc/parser/mineru_parser.py` | 860-960 | 解析 PDF 主方法 |
| `_read_output()` | `deepdoc/parser/mineru_parser.py` | 611-680 | 读取 JSON 输出 |
| `_transfer_to_sections()` | `deepdoc/parser/mineru_parser.py` | 797-855 | 转换为 sections |
| `_line_tag()` | `deepdoc/parser/mineru_parser.py` | 399-424 | 生成位置标签 |
| `extract_positions()` | `deepdoc/parser/mineru_parser.py` | 586-609 | 提取位置信息 |
| `crop()` | `deepdoc/parser/mineru_parser.py` | 426-580 | 裁剪图片 |
| `image2id()` | `rag/utils/base64_image.py` | 28-59 | 上传图片 |
| `id2image()` | `rag/utils/base64_image.py` | 62-76 | 下载图片 |
| `naive_merge_with_images()` | `rag/nlp/__init__.py` | 922-1090 | 合并成 chunks |
| `add_chunk()` | `rag/nlp/__init__.py` | 958-1029 | 添加文本和图片到 chunk |
| `concat_img()` | `rag/nlp/__init__.py` | 1106-1150 | 垂直拼接图片 |

---

## 九、数据流向图

```
MinerU JSON 输出
    ↓
_transfer_to_sections() → Section (文本, 位置标签)
    ↓
parser.py: _pdf()
    ├─ crop() → PIL.Image（从 PDF 裁剪）
    └─ extract_positions() → positions
    ↓
Bbox {text, image, positions}
    ↓
image2id() → 上传到对象存储
    ↓
JSON {text, img_id, position_tag, positions}
    ↓
splitter.py: _invoke()
    ├─ id2image() → 从对象存储下载
    └─ naive_merge_with_images()
        └─ add_chunk() → 合并文本和图片
    ↓
Chunk {text, image, positions}
```

---

本文档详细解析了 MinerU 解析出的 TEXT、TABLE、IMAGE 从 section 到 chunk 的完整转化过程，包括每个函数的代码、调用关系和数据流向。

