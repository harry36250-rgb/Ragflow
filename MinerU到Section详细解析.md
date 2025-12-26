# MinerU 解析块到 Section 的详细对应关系

## 一、MinerU 的输出格式

### 1.1 JSON 文件结构

MinerU 解析完成后，会在输出目录生成 `{file_stem}_content_list.json` 文件，包含一个 JSON 数组。

**文件位置：**
- 路径：`<output_dir>/<file_stem>/<method>/<file_stem>_content_list.json`
- 例如：`/tmp/mineru_pdf_xxx/test/auto/test_content_list.json`

**JSON 数组结构：**
```json
[
    {
        "type": "text",           // 内容类型
        "text": "这是文本内容",    // 文本内容（TEXT 类型）
        "bbox": [x0, top, x1, bottom],  // 边界框（相对坐标 0-1000）
        "page_idx": 0,            // 页码（从 0 开始）
        "text_level": 1           // 文本层级（可选）
    },
    {
        "type": "image",
        "img_path": "images/xxx.jpg",  // 图片相对路径
        "image_caption": ["图片标题1", "图片标题2"],  // 图片标题列表
        "image_footnote": ["脚注1"],  // 图片脚注列表
        "bbox": [792, 192, 967, 342],
        "page_idx": 3
    },
    {
        "type": "table",
        "table_body": "表格内容...",
        "table_caption": ["表格标题"],
        "table_footnote": ["表格脚注"],
        "table_img_path": "tables/xxx.jpg",
        "bbox": [100, 200, 500, 400],
        "page_idx": 5
    }
]
```

### 1.2 内容类型枚举

**定义位置：** `deepdoc/parser/mineru_parser.py:46-53`

```python
class MinerUContentType(StrEnum):
    IMAGE = "image"        # 图片
    TABLE = "table"        # 表格
    TEXT = "text"          # 文本
    EQUATION = "equation"  # 公式
    CODE = "code"          # 代码
    LIST = "list"          # 列表
    DISCARDED = "discarded"  # 丢弃的内容
```

---

## 二、读取 MinerU 输出

### 2.1 读取 JSON 文件

**方法：** `_read_output()`  
**位置：** `deepdoc/parser/mineru_parser.py:611-677`

**流程：**

```python
def _read_output(self, output_dir, file_stem, method, backend):
    # 1. 构建候选路径列表（根据 backend 和 method）
    candidates = [
        output_dir / file_stem / "vlm",      # VLM 后端优先
        output_dir / file_stem / method,     # 方法目录
        output_dir / file_stem / "auto"      # 默认目录
    ]
    
    # 2. 查找 JSON 文件
    for sub in candidates:
        json_file = sub / f"{file_stem}_content_list.json"
        if json_file.exists():
            break
    
    # 3. 读取并解析 JSON
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)  # data 是一个列表，每个元素是一个字典
    
    # 4. 将相对路径转换为绝对路径
    for item in data:
        for key in ("img_path", "table_img_path", "equation_img_path"):
            if key in item and item[key]:
                # 将相对路径转换为绝对路径
                item[key] = str((subdir / item[key]).resolve())
    
    return data  # 返回解析后的列表
```

**关键点：**
- 返回的 `data` 是一个列表，每个元素对应 MinerU 识别的一个内容块
- 图片路径从相对路径转换为绝对路径，便于后续访问

---

## 三、转换为 Sections

### 3.1 转换方法

**方法：** `_transfer_to_sections()`  
**位置：** `deepdoc/parser/mineru_parser.py:797-855`

### 3.2 转换规则（按类型）

#### 3.2.1 TEXT 类型（文本）

```python
case MinerUContentType.TEXT:
    section = output["text"]  # 直接使用文本内容
```

**示例：**
```python
# MinerU 输出：
{
    "type": "text",
    "text": "安全须知",
    "bbox": [143, 154, 540, 279],
    "page_idx": 0
}

# 转换后的 section：
section = "安全须知"
```

#### 3.2.2 IMAGE 类型（图片）

```python
case MinerUContentType.IMAGE:
    image_path = output.get("img_path", "")
    processed_result = ""
    
    if image_path:
        # 调用 Dify 工作流生成图片描述（如果 image_caption 为空）
        processed_result = self._process_image_path(image_path)
        caption = "".join(output.get("image_caption", [])) or processed_result
    else:
        caption = "".join(output.get("image_caption", []))
    
    # 组合：caption + footnote
    section = caption + "\n" + "".join(output.get("image_footnote", []))
```

**示例：**
```python
# MinerU 输出：
{
    "type": "image",
    "img_path": "/tmp/.../images/xxx.jpg",
    "image_caption": [],  # 空列表
    "image_footnote": [],
    "bbox": [792, 192, 967, 342],
    "page_idx": 3
}

# 转换过程：
# 1. 调用 _process_image_path() → 返回 Dify 生成的描述
# 2. caption = Dify 生成的描述（因为 image_caption 为空）
# 3. section = caption + "\n" + "" = Dify 生成的描述

# 转换后的 section：
section = "图片展示了一台黑色的电子设备，设备背面有多个接口..."
```

#### 3.2.3 TABLE 类型（表格）

```python
case MinerUContentType.TABLE:
    section = (
        output.get("table_body", "") +           # 表格主体
        "\n".join(output.get("table_caption", [])) +  # 表格标题（列表转字符串）
        "\n".join(output.get("table_footnote", []))    # 表格脚注（列表转字符串）
    )
    if not section.strip():
        section = "FAILED TO PARSE TABLE"  # 解析失败时的占位文本
```

**示例：**
```python
# MinerU 输出：
{
    "type": "table",
    "table_body": "列1\t列2\n值1\t值2",
    "table_caption": ["表1：数据统计"],
    "table_footnote": ["注：数据来源..."],
    "bbox": [100, 200, 500, 400],
    "page_idx": 5
}

# 转换后的 section：
section = "列1\t列2\n值1\t值2\n表1：数据统计\n注：数据来源..."
```

#### 3.2.4 EQUATION 类型（公式）

```python
case MinerUContentType.EQUATION:
    section = output["text"]  # 直接使用文本内容
```

#### 3.2.5 CODE 类型（代码）

```python
case MinerUContentType.CODE:
    section = (
        output["code_body"] +                    # 代码主体
        "\n".join(output.get("code_caption", []))  # 代码标题
    )
```

#### 3.2.6 LIST 类型（列表）

```python
case MinerUContentType.LIST:
    section = "\n".join(output.get("list_items", []))  # 列表项用换行符连接
```

#### 3.2.7 DISCARDED 类型（丢弃）

```python
case MinerUContentType.DISCARDED:
    pass  # 跳过，不生成 section
```

---

## 四、位置标签生成

### 4.1 位置标签格式

**方法：** `_line_tag()`  
**位置：** `deepdoc/parser/mineru_parser.py:319-331`

**格式：** `@@页码\tx0\tx1\ttop\tbottom##`

**示例：** `@@4\t100.0\t200.0\t50.0\t150.0##`

### 4.2 生成过程

```python
def _line_tag(self, bx):
    # 1. 获取页码（从 0 开始转为从 1 开始）
    pn = [bx["page_idx"] + 1]
    
    # 2. 获取边界框（相对坐标 0-1000）
    positions = bx.get("bbox", (0, 0, 0, 0))
    x0, top, x1, bott = positions
    
    # 3. 如果已加载页面图片，转换为绝对像素坐标
    if hasattr(self, "page_images") and self.page_images:
        page_width, page_height = self.page_images[bx["page_idx"]].size
        # MinerU 使用 0-1000 的相对坐标，转换为像素坐标
        x0 = (x0 / 1000.0) * page_width
        x1 = (x1 / 1000.0) * page_width
        top = (top / 1000.0) * page_height
        bott = (bott / 1000.0) * page_height
    
    # 4. 生成位置标签字符串
    return "@@{}\t{:.1f}\t{:.1f}\t{:.1f}\t{:.1f}##".format(
        "-".join([str(p) for p in pn]),  # 页码（支持范围，如 "1-3"）
        x0, x1, top, bott
    )
```

**关键点：**
- 页码从 0 开始转为从 1 开始
- 坐标从相对坐标（0-1000）转换为绝对像素坐标（如果有页面图片）
- 支持跨页范围（如 "1-3" 表示第 1、2、3 页）

---

## 五、不同 parse_method 的输出格式

### 5.1 默认模式（parse_method = None 或 "raw"）

**格式：** `(文本内容, 位置标签)`

```python
# 默认模式
sections.append((section, self._line_tag(output)))
```

**示例：**
```python
# 输入：
section = "安全须知"
position_tag = "@@1\t143.0\t540.0\t154.0\t279.0##"

# 输出：
("安全须知", "@@1\t143.0\t540.0\t154.0\t279.0##")
```

### 5.2 manual 模式（parse_method = "manual"）

**格式：** `(文本内容, 类型, 位置标签)`

```python
# manual 模式
sections.append((section, output["type"], self._line_tag(output)))
```

**示例：**
```python
# 输入：
section = "图片描述..."
type = "image"
position_tag = "@@4\t792.0\t967.0\t192.0\t342.0##"

# 输出：
("图片描述...", "image", "@@4\t792.0\t967.0\t192.0\t342.0##")
```

### 5.3 paper 模式（parse_method = "paper"）

**格式：** `(文本内容+位置标签, 类型)`

```python
# paper 模式：位置标签附加到文本末尾
sections.append((section + self._line_tag(output), output["type"]))
```

**示例：**
```python
# 输入：
section = "安全须知"
position_tag = "@@1\t143.0\t540.0\t154.0\t279.0##"
type = "text"

# 输出：
("安全须知@@1\t143.0\t540.0\t154.0\t279.0##", "text")
```

---

## 六、完整转换流程示例

### 6.1 示例：单个图片块

**MinerU 输出：**
```json
{
    "type": "image",
    "img_path": "/tmp/.../images/493c0926...jpg",
    "image_caption": [],
    "image_footnote": [],
    "bbox": [792, 192, 967, 342],
    "page_idx": 3
}
```

**转换步骤：**

1. **提取图片路径：**
   ```python
   image_path = "/tmp/.../images/493c0926...jpg"
   ```

2. **调用 Dify 生成描述：**
   ```python
   processed_result = self._process_image_path(image_path)
   # 返回： "图片展示了一台黑色的电子设备，设备背面有多个接口..."
   ```

3. **组合 section 文本：**
   ```python
   caption = processed_result  # 因为 image_caption 为空
   section = caption + "\n" + "" = processed_result
   ```

4. **生成位置标签：**
   ```python
   position_tag = self._line_tag(output)
   # 返回： "@@4\t792.0\t967.0\t192.0\t342.0##"
   ```

5. **生成 section（默认模式）：**
   ```python
   section_tuple = (section, position_tag)
   # 结果： ("图片展示了一台黑色的电子设备...", "@@4\t792.0\t967.0\t192.0\t342.0##")
   ```

### 6.2 示例：多个块转换

**MinerU 输出列表：**
```json
[
    {"type": "text", "text": "安全须知", "bbox": [143, 154, 540, 279], "page_idx": 0},
    {"type": "image", "img_path": "...", "image_caption": [], "bbox": [792, 192, 967, 342], "page_idx": 3},
    {"type": "text", "text": "严禁任何时候用双手同时触摸电池箱体的正负极柱。", "bbox": [100, 400, 500, 450], "page_idx": 0}
]
```

**转换后的 sections 列表：**
```python
[
    ("安全须知", "@@1\t143.0\t540.0\t154.0\t279.0##"),
    ("图片展示了一台黑色的电子设备...", "@@4\t792.0\t967.0\t192.0\t342.0##"),
    ("严禁任何时候用双手同时触摸电池箱体的正负极柱。", "@@1\t100.0\t500.0\t400.0\t450.0##")
]
```

---

## 七、关键代码位置

| 步骤 | 文件 | 方法/函数 | 行数 |
|------|------|----------|------|
| 读取 JSON | `deepdoc/parser/mineru_parser.py` | `_read_output()` | 611-677 |
| 类型转换 | `deepdoc/parser/mineru_parser.py` | `_transfer_to_sections()` | 797-855 |
| 位置标签生成 | `deepdoc/parser/mineru_parser.py` | `_line_tag()` | 319-331 |
| 图片处理 | `deepdoc/parser/mineru_parser.py` | `_process_image_path()` | 675-795 |

---

## 八、总结

### 8.1 一对一映射关系

- **每个 MinerU 输出块** → **一个 section**
- **每个 section** = `(文本内容, 位置标签)` 或 `(文本内容, 类型, 位置标签)`

### 8.2 文本内容提取规则

| 类型 | 提取规则 |
|------|----------|
| TEXT | 直接使用 `text` 字段 |
| IMAGE | `image_caption` + `image_footnote`（如果 caption 为空，调用 Dify 生成） |
| TABLE | `table_body` + `table_caption` + `table_footnote` |
| EQUATION | 直接使用 `text` 字段 |
| CODE | `code_body` + `code_caption` |
| LIST | `list_items` 用换行符连接 |
| DISCARDED | 跳过，不生成 section |

### 8.3 位置标签规则

- 格式：`@@页码\tx0\tx1\ttop\tbottom##`
- 页码：从 0 开始转为从 1 开始
- 坐标：从相对坐标（0-1000）转换为绝对像素坐标（如果有页面图片）
- 支持跨页范围（如 "1-3"）

### 8.4 输出格式规则

- **默认模式**：`(文本, 位置标签)`
- **manual 模式**：`(文本, 类型, 位置标签)`
- **paper 模式**：`(文本+位置标签, 类型)`

