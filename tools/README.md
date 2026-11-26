# 软件源构建

`make_index.py` 生成 `jzksnsjswkw/repo` 这个 APT 源的索引，两个项目共用：
Pusher 和 Relocate 各自发版时调用同一份脚本，谁发版谁就把别人的条目带过去。

## 为什么共用一份

`gh-pages` 分支被两个项目共同发布，每次发布都是整目录替换
（`keep_files: false`）。如果每个项目自带一份脚本，两边各写一遍
「向前继承」的逻辑，迟早有一边改漏——那一次发布的版本就会把另一个项目从
源里抹掉。

## 新增一个项目

在 `PROJECTS` 里加一项，别的都不用动：

```python
PROJECTS = {
    "pusher": {
        "releases": "https://github.com/jzksnsjswkw/Pusher-rootless/releases/download",
        "icons": {},
        "obsolete": {"com.jzksnsjswkw.pusher"},
        "depiction": {"com.jzksnsjswkw.pusher-rootless": "pusher.json"},
    },
}
```

`obsolete` 只放本项目自己的旧包名，不要放别人的——一个项目的历史不该
让另一个项目的清理逻辑删掉。

## 调用方式

项目的 workflow 先把仓库的 `tools/` 和线上索引拉到工作区，然后：

```bash
python3 tools/make_index.py \
    --project pusher \
    --tag v3.0.1 \
    --out repo \
    --previous previous/Packages \
    --debs staged/*.deb
```

`--previous` 是可选的：第一次发布、线上还没有索引时省略即可。

## 需要的依赖

- `python-debian`（`pip3 install python-debian`）
- `dpkg-deb`（macOS 上 `brew install dpkg`）
- `zstd`（可选；没有就生成 `Packages.zst` 之外的四种压缩格式）
