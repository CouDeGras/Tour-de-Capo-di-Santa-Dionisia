# 吴语界面用字

内部语言选项为 `zhs`，显示“吴语”，采用简体字、上海话用词和语气。网页语言为 `wuu-Hans`；日期格式和 Nominatim 请求使用 `zh-Hans`。地点专名保持地名，不逐字改成方言。

按用户指定采用“言话”（语言）、“搭”（与、和）、“辰光”（时间）、“场化”（地点）。技术气象名词保留明确含义；提示语、连接词、否定和动作采用吴语表达，不靠简繁转换生成方言文案。

采用词源拼写，不采用纯记音借字；再转换成简体字。白天写“日里”（用户确认“日裡”须简化），进行体写“在丄”，不用“勒浪”。无没采用“無沒”的简体；指示词采用“箇”的简体“个”，与所有格“个”按语境区分。

不确定用字查阅维基词典，2026-11-10 核对：

- [上海言話](https://en.wiktionary.org/wiki/上海言話)：上海閒話的另一写法；界面遵从用户指定的“言话”。
- [搭](https://en.wiktionary.org/wiki/搭)：北部吴语“和、与”。
- [辰光](https://en.wiktionary.org/wiki/辰光)、[場化](https://en.wiktionary.org/wiki/場化)：时间、地点。
- [關脫](https://en.wiktionary.org/wiki/關脫)、[嘸沒](https://en.wiktionary.org/wiki/嘸沒)：关掉、没有；采用“关脱”及词源形式“无没”。
- [搿歇](https://en.wiktionary.org/wiki/搿歇)：现在。
- [儕](https://en.wiktionary.org/wiki/儕)：都、全部；简体作“侪”。
- [吴语书写规范](https://zh.wiktionary.org/zh-hans/Wiktionary:AZH/Wu)：区分词源拼写与语音拼写。
- [在丄](https://zh.wiktionary.org/zh-hans/在丄)：由“在”“上”弱化而来，也表进行体。
- [哉](https://en.wiktionary.org/wiki/哉)：吴语句末时体助词。
- [𠲎](https://en.wiktionary.org/wiki/𠲎)：是非问助词，按词源拼写取“否”，不用记音字“伐”。
- [洋涇浜](https://en.wiktionary.org/wiki/洋涇浜)：例句记载“有眼”的上海话用法。
- [落水](https://en.wiktionary.org/wiki/落水)：吴语等方言的下雨说法。

地点名称的简化使用 OpenCC，包含数据库里已经缓存的名称；不因切换语言重查 Nominatim。英文副标题保持英文。天气状态按状态代码从当前语言目录取文案，保存的天气结果不决定可用语言。

天气状态采用简练气象标签，例如“少云”“局部多云”“阵雨”“小雪”“无显著云层”，不用“有眼云”“有眼日头”或“落小雨”等口语化描述。“毛毛雨”“冻毛毛雨”是气象术语，保留。“日头轨迹”为用户明确指定的标签；设置中同样采用“日头轨迹样式”。


# 粵語界面

繁體中文選項改為書面粵語，選單顯示「粵語」。使用「同」連接並列項目，「緊」標記進行中的動作，「嘅」連接修飾語；操作提示採自然粵語，氣象與天文標籤維持精簡術語。地名仍選擇繁體中文名稱，沿用 Nominatim 的 `zh-Hant`，保留既有 TC 字體、圖表及等寬字體設定。

用字參考維基詞典：[同](https://en.wiktionary.org/wiki/同)、[緊](https://en.wiktionary.org/wiki/緊)、[嘅](https://en.wiktionary.org/wiki/嘅)。
