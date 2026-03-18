# cit 中文文档

> Git 风格的对话分支管理，为 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 设计。
>
> English docs: [README.md](../README.md)

## 命令

```
cit branch <label>      创建子分支
cit switch <branch>     切换分支
cit squash <summary>    写入摘要，自动切回父分支
cit close [branch]      关闭分支的 pane（数据保留）
cit log                 查看分支树
cit inbox [branch]      查看子分支摘要
cit status              统计信息
cit init                注册当前会话（通常自动完成）
cit cleanup [hours]     清理过期占位符
```

## 状态图标

| 图标 | 含义 |
|------|------|
| ● | 探索中（pane 活着，无摘要） |
| ◆ | squashed + 活跃 |
| ◈ | squashed + 挂起 |
| ○ | 挂起 |

## 完整示例：递归式学习 Transformer

场景：你在读一篇 KV Cache 优化的论文，遇到不懂的概念就开分支去学，学完带结论回来继续。

```
┌─ main ──────────────────────────────────────────────────────────┐
│ 你: 帮我读这篇 PagedAttention 论文                                │
│ Claude: 这篇论文核心思路是把 KV Cache 按页管理...用到了             │
│         Multi-Head Attention 和 Beam Search...                    │
│ 你: 等等，Multi-Head Attention 具体怎么工作的？我想搞清楚           │
│                                                                   │
│ > cit branch learn-mha                                           │
│   → 右边开新 pane，专门学 MHA                                     │
└───────────────────────────────────────────────────────────────────┘

┌─ learn-mha ─────────────────────────────────────────────────────┐
│ 你: 从头给我讲 Multi-Head Attention                               │
│ Claude: MHA 的核心是把 Q/K/V 投影到多个子空间... Scaled Dot-Product │
│         Attention 的公式是 softmax(QK^T / √d_k)V...               │
│ 你: softmax 里为什么要除以 √d_k？                                  │
│                                                                   │
│ > cit branch why-sqrt-dk                                         │
│   → 再往右开一层，递归深挖                                         │
│                                                                   │
│ 此时 tmux:                                                        │
│ ┌──────────┬──────────┬──────────┐                                │
│ │          │          │          │                                │
│ │  main    │ learn-mha│why-sqrt  │                                │
│ │          │          │          │                                │
│ └──────────┴──────────┴──────────┘                                │
└───────────────────────────────────────────────────────────────────┘

┌─ why-sqrt-dk ───────────────────────────────────────────────────┐
│ 你: 为什么 attention 要除以 √d_k                                  │
│ Claude: 因为 d_k 很大时 QK^T 的值会很大，softmax 梯度会消失...     │
│ 你: 懂了                                                          │
│                                                                   │
│ > cit squash                                                     │
│   Claude 自动生成:                                                │
│   "除以 √d_k 是为了防止点积随维度增大，导致 softmax 进入            │
│    饱和区梯度消失。这是一个方差归一化技巧。"                         │
│   → 自动切回 learn-mha                                            │
└───────────────────────────────────────────────────────────────────┘

┌─ 回到 learn-mha ────────────────────────────────────────────────┐
│ > cit inbox                                                      │
│ 📬 why-sqrt-dk: 除以 √d_k 防止点积过大导致 softmax 梯度消失...    │
│                                                                   │
│ Claude:（拿到子分支结论，继续讲 MHA）                               │
│ 你: OK 我理解 MHA 了                                               │
│                                                                   │
│ > cit squash                                                     │
│   "MHA 将 Q/K/V 投影到 h 个子空间并行做 attention 再拼接。          │
│    每个 head 捕捉不同层面的依赖关系。√d_k 归一化防止梯度消失。"      │
│   → 自动切回 main                                                 │
└───────────────────────────────────────────────────────────────────┘

┌─ 回到 main ────────────────────────────────────────────────────┐
│ > cit inbox                                                     │
│ 📬 learn-mha: MHA 将 Q/K/V 投影到 h 个子空间并行做 attention...  │
│                                                                   │
│ Claude:（拿到 MHA 结论，继续讲 PagedAttention 论文）               │
│                                                                   │
│ > cit close learn-mha                                            │
│ > cit close why-sqrt-dk                                          │
│                                                                   │
│ > cit log                                                        │
│ main ●                                                           │
│ └─ learn-mha ◈                                                   │
│    └─ why-sqrt-dk ◈                                              │
│                                                                   │
│ 三层递归学习，每层的结论逐级冒泡回主线。                            │
│ 主线 context 里只有精炼的摘要，没有中间的学习过程。                  │
└───────────────────────────────────────────────────────────────────┘
```

**为什么不用 sub-agent？**

Sub-agent 适合"帮我查一下然后告诉我结果"这种委托任务。
但学习是**人驱动**的——你要提问、追问、说"等等这里没懂"，节奏由你控制。
cit 保留了这种交互性，同时解决了"学完回来忘了主线在干嘛"的问题。

**核心价值**：每次 squash 把探索过程压缩成结论，逐级冒泡。主线只看到精炼的摘要，不被中间过程污染 context。

## 命令速查

| 命令 | 用途 | 示例 |
|------|------|------|
| `cit branch <label>` | 开子分支 | `cit branch fix-oom` |
| `cit switch <branch>` | 切换/重开分支 | `cit switch fix-oom` |
| `cit squash` | 总结当前分支，切回父 | 直接说 `cit squash` |
| `cit close [branch]` | 关 pane，保留数据 | `cit close fix-oom` |
| `cit log` | 看分支树 | `cit log` |
| `cit inbox [branch]` | 看子分支摘要 | `cit inbox` |
| `cit status` | 统计信息 | `cit status` |
| `cit cleanup` | 清理过期占位 | `cit cleanup 24` |

## 状态图标

| 图标 | 含义 |
|------|------|
| ● | 探索中（pane 活着，无摘要） |
| ◆ | squashed + 活跃 |
| ◈ | squashed + 挂起 |
| ○ | 挂起（pane 关了，无摘要） |

## 布局规则（tmux）

- 子分支 → 水平分割父 pane（出现在右边）
- 同级分支 → 垂直分割兄弟 pane（出现在下方）

```
1父2子:
┌──────────┬──────────┐
│          │ child-A  │
│  main    ├──────────┤
│          │ child-B  │
└──────────┴──────────┘

1父1子1孙:
┌──────┬──────┬──────┐
│      │      │      │
│ main │ child│grandch│
│      │      │      │
└──────┴──────┴──────┘
```

## 清理

```bash
rm data/cit.db    # 重置所有数据
```
