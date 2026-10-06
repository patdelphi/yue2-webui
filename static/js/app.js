        console.log('YuE2 scripts loading...');

        function initAccordionCollapse() {
            var textareas = document.querySelectorAll('textarea');
            if (textareas.length < 2) { setTimeout(initAccordionCollapse, 500); return; }
            setTimeout(function() {
                var buttons = document.querySelectorAll('button.label-wrap');
                var labelsToClose = ['风格标签', '歌词工具', '音频后处理', '高级采样参数', '生成的乐谱', '转谱结果'];
                var clickedCount = 0;
                for (var i = 0; i < buttons.length; i++) {
                    var btn = buttons[i];
                    var text = btn.textContent.trim();
                    for (var j = 0; j < labelsToClose.length; j++) {
                        if (text.indexOf(labelsToClose[j]) !== -1) {
                            if (btn.classList.contains('open')) {
                                btn.click();
                                clickedCount++;
                            }
                            break;
                        }
                    }
                }
                console.log('Accordions auto-collapsed:', clickedCount);
            }, 1000);
        }
        initAccordionCollapse();

        window.initHistoryTableClick = function() {
            var tableContainer = document.querySelector('#history-table');
            if (!tableContainer) { setTimeout(window.initHistoryTableClick, 500); return; }
            // 该函数会被 initPlayerZoom 与 2s 定时器各调用一次，加标记避免重复挂监听/插样式
            if (tableContainer.dataset.y2RowClickInit === '1') return;
            tableContainer.dataset.y2RowClickInit = '1';

            // Gradio 6 的数据表用虚拟滚动：表头是 <thead><tr role="row">，数据行是
            // <div class="virtual-row" role="row">（不是 tbody tr），tbody 内仅有一条 0 高的量宽占位 tr。
            // 故按 role="row" 收集数据行，并用“是否含列头单元格”排除表头行。
            function dataRows() {
                return Array.from(tableContainer.querySelectorAll('[role="row"]'))
                    .filter(function(r) { return !r.querySelector('[role="columnheader"]'); });
            }

            // 选行本身交给 Gradio 原生 Dataframe.select（app.py 的 history_df.select）；
            // 这里只负责行高亮，避免与原生 select 重复触发后端加载。
            tableContainer.addEventListener('click', function(e) {
                var row = e.target.closest('[role="row"]');
                if (!row) return;

                var rows = dataRows();
                if (rows.indexOf(row) < 0) return;   // 表头行 / 量宽占位行

                rows.forEach(function(r) { r.style.background = ''; });
                row.style.background = 'rgba(59,130,246,0.2)';
            });

            var rowSel = '#history-table [role="row"]:not(:has([role="columnheader"]))';
            var style = document.createElement('style');
            style.textContent = rowSel + ' { cursor: pointer; } ' + rowSel + ':hover { background: rgba(59,130,246,0.1) !important; }';
            document.head.appendChild(style);
            console.log('History table click handler initialized');
        };

        (function() {
            console.log('YuE2 scripts loaded');

            function getLyricsTextarea() {
                return document.querySelector('textarea[placeholder*="[Verse]"]');
            }

            function triggerUpdate(ta) {
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                ta.dispatchEvent(new Event('change', { bubbles: true }));
            }

            function insertSegment(name) {
                const ta = getLyricsTextarea();
                if (!ta) return;
                const marker = '\n[' + name + ']\n';
                const pos = ta.selectionStart || ta.value.length;
                const before = ta.value.substring(0, pos);
                const after = ta.value.substring(pos);
                const needNL = before.length > 0 && !before.endsWith('\n');
                ta.value = before + (needNL ? '\n' : '') + marker + after;
                triggerUpdate(ta);
                setTimeout(updateSegmentDisplay, 100);
            }

            function applyStructure(segments) {
                const ta = getLyricsTextarea();
                if (!ta) return;
                let text = '';
                for (const seg of segments) {
                    text += '[' + seg + ']\n\n';
                }
                ta.value = text.trim();
                triggerUpdate(ta);
                setTimeout(updateSegmentDisplay, 100);
            }

            const segColors = {
                'verse': '#4a90d9', 'chorus': '#e67e22', 'bridge': '#27ae60',
                'intro': '#95a5a6', 'outro': '#7f8c8d', 'pre-chorus': '#8e44ad',
            };

            function getSegColor(name) {
                return segColors[name.toLowerCase().replace(/\s/g, '-')] || '#666';
            }

            function updateSegmentDisplay() {
                const ta = getLyricsTextarea();
                if (!ta) return;
                const text = ta.value;
                if (!text.trim()) {
                    const container = document.getElementById('segment-cards');
                    if (container) container.innerHTML = '';
                    return;
                }

                const segments = [];
                const lines = text.split('\n');
                let current = { name: 'Intro', lines: [], content: '' };

                for (const line of lines) {
                    const trimmed = line.trim();
                    if (trimmed.startsWith('//') || trimmed.startsWith('**')) continue;
                    if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
                        if (current.name !== 'Intro' || current.content.trim() || segments.length > 0) {
                            segments.push(current);
                        }
                        current = { name: trimmed.slice(1, -1), lines: [], content: '' };
                    } else {
                        if (trimmed) {
                            current.lines.push(trimmed);
                            current.content += line + '\n';
                        }
                    }
                }
                segments.push(current);

                const container = document.getElementById('segment-cards');
                if (!container) return;
                container.innerHTML = '';

                for (const seg of segments) {
                    const card = document.createElement('div');
                    card.className = 'segment-card';
                    card.dataset.name = seg.name;
                    card.dataset.content = seg.content.trim();
                    card.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 10px;' +
                        'background:var(--background-fill-secondary, rgba(255,255,255,0.05));border-radius:6px;margin:3px 0;' +
                        'border-left:3px solid ' + getSegColor(seg.name) + ';cursor:grab;';

                    const handle = document.createElement('span');
                    handle.textContent = '\u283F';
                    handle.style.cssText = 'cursor:grab;color:#666;font-size:16px;';

                    const label = document.createElement('span');
                    label.textContent = seg.name;
                    label.style.cssText = 'font-weight:bold;color:' + getSegColor(seg.name) + ';min-width:80px;';

                    const info = document.createElement('span');
                    info.textContent = seg.lines.length + ' \u884C';
                    info.style.cssText = 'color:#888;font-size:12px;';

                    card.appendChild(handle);
                    card.appendChild(label);
                    card.appendChild(info);
                    container.appendChild(card);
                }

                if (typeof Sortable !== 'undefined' && container.children.length > 1) {
                    if (container._sortable) container._sortable.destroy();
                    container._sortable = Sortable.create(container, {
                        handle: '.segment-card span:first-child',
                        animation: 150,
                        ghostClass: 'segment-ghost',
                        onEnd: function(evt) {
                            const cards = container.querySelectorAll('.segment-card');
                            let text = '';
                            cards.forEach(function(c) {
                                text += '[' + c.dataset.name + ']\n';
                                if (c.dataset.content) text += c.dataset.content + '\n';
                                text += '\n';
                            });
                            ta.value = text.trim();
                            triggerUpdate(ta);
                        }
                    });
                }
            }

            const segBtns = { '+ Verse': 'Verse', '+ Chorus': 'Chorus', '+ Bridge': 'Bridge',
                '+ Intro': 'Intro', '+ Outro': 'Outro', '+ Pre-Chorus': 'Pre-Chorus' };
            const structTemplates = {
                'Verse-Chorus': ['Verse', 'Chorus', 'Verse', 'Chorus'],
                'V-C-V-C': ['Verse', 'Chorus', 'Verse', 'Chorus'],
                'V-C-V-C-B-C': ['Verse', 'Chorus', 'Verse', 'Chorus', 'Bridge', 'Chorus'],
                'V-V-C': ['Verse', 'Verse', 'Chorus'],
                'A-A-B-A': ['Verse', 'Verse', 'Bridge', 'Verse'],
            };

            function initButtonWiring() {
                const allBtns = document.querySelectorAll('button');
                let wired = false;
                allBtns.forEach(function(btn) {
                    const t = btn.textContent.trim();
                    if (segBtns[t]) {
                        btn.addEventListener('click', function() { insertSegment(segBtns[t]); });
                        wired = true;
                    }
                    if (structTemplates[t]) {
                        btn.addEventListener('click', function() { applyStructure(structTemplates[t]); });
                        wired = true;
                    }
                });
                if (!wired) setTimeout(initButtonWiring, 500);
            }
            initButtonWiring();

            function initLyricsEditor() {
                const ta = getLyricsTextarea();
                if (!ta) { setTimeout(initLyricsEditor, 500); return; }
                updateSegmentDisplay();
                let timer;
                ta.addEventListener('input', function() {
                    clearTimeout(timer);
                    timer = setTimeout(updateSegmentDisplay, 600);
                });
                // Gradio's programmatic value updates (e.g. the 使用上一次
                // restore button) bypass DOM input events, so hook the setter.
                const desc = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
                if (desc && desc.set && !ta.__yuE2ValuePatched) {
                    const origGet = desc.get, origSet = desc.set;
                    Object.defineProperty(ta, 'value', {
                        get: function() { return origGet.call(this); },
                        set: function(v) {
                            origSet.call(this, v);
                            this.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                    });
                    ta.__yuE2ValuePatched = true;
                }
            }
            initLyricsEditor();

            // ---------------------------------------------------------------
            // 统一的 ABC 预览渲染器（B3：合并生成页/历史页/转谱页三套近似实现）
            // 公共的防抖/轮询/重试/渲染逻辑只保留一份，各页差异通过 opts 参数化。
            // ---------------------------------------------------------------

            // 展开「生成的乐谱」折叠区（仅生成页需要，渲染前调用）
            function ensureAbcAccordionOpen() {
                var buttons = document.querySelectorAll('button.label-wrap');
                for (var i = 0; i < buttons.length; i++) {
                    var btn = buttons[i];
                    if (btn.textContent.includes('生成的乐谱')) {
                        if (!btn.classList.contains('open')) { btn.click(); }
                        break;
                    }
                }
            }

            // 按容器 id 定位其中的 ABC 文本框（生成页 / 历史页共用形态）
            function findAbcTextareaById(id) {
                var el = document.getElementById(id);
                if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) return el;
                if (el) {
                    var tab = el.querySelector('textarea, input[type="text"]');
                    if (tab) return tab;
                }
                return document.querySelector('#' + id + ' textarea, #' + id + ' input[type="text"]');
            }

            // 绑定「导出 MIDI / 导出 PNG」按钮（仅生成页）
            function bindAbcExportButtons(findTextarea, paperId) {
                document.querySelectorAll('button').forEach(function(btn) {
                    if (btn.textContent.includes('\u5BFC\u51FA MIDI')) {
                        btn.onclick = function() {
                            var ta = findTextarea();
                            const abcText = ta ? ta.value : '';
                            if (abcText && abcText.trim()) {
                                const midiData = ABCJS.synth.createSynth(abcText);
                                const blob = new Blob([midiData], {type: 'audio/midi'});
                                const url = URL.createObjectURL(blob);
                                const a = document.createElement('a');
                                a.href = url;
                                a.download = 'score.mid';
                                a.click();
                                URL.revokeObjectURL(url);
                            }
                        };
                    }
                    if (btn.textContent.includes('\u5BFC\u51FA PNG')) {
                        btn.onclick = function() {
                            const svg = document.querySelector('#' + paperId + ' svg');
                            if (svg) {
                                const svgData = new XMLSerializer().serializeToString(svg);
                                const canvas = document.createElement('canvas');
                                const ctx = canvas.getContext('2d');
                                const img = new Image();
                                img.onload = function() {
                                    canvas.width = img.width;
                                    canvas.height = img.height;
                                    ctx.drawImage(img, 0, 0);
                                    const pngUrl = canvas.toDataURL('image/png');
                                    const a = document.createElement('a');
                                    a.href = pngUrl;
                                    a.download = 'score.png';
                                    a.click();
                                };
                                img.src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svgData)));
                            }
                        };
                    }
                });
            }

            // 核心渲染器。opts：
            //   retry              重试入口（ABCJS/容器/文本框未就绪时 500ms 后重调）
            //   paperId/audioId    ABCJS 乐谱/播放器容器 id
            //   findTextarea       () => textarea|null，定位 ABC 文本来源
            //   requirePaper       true 时容器缺失也重试（历史页/转谱页）
            //   ensureAccordion    true 时渲染前展开「生成的乐谱」（生成页）
            //   liveInput          true 时监听 input 事件并防抖渲染（生成页）
            //   clearWhenEmpty     空文本时清空容器（历史页/转谱页）
            //   hidePlaceholderId  非空文本时隐藏该容器内的居中占位（转谱页）
            //   bindExports        true 时绑定导出 MIDI/PNG（生成页）
            //   errorLabel         渲染异常时的 console 前缀
            function initAbcPreviewCore(opts) {
                if (typeof ABCJS === 'undefined') {
                    setTimeout(opts.retry, 500);
                    return;
                }
                var paper = document.getElementById(opts.paperId);
                if (opts.requirePaper && !paper) {
                    setTimeout(opts.retry, 500);
                    return;
                }
                var textarea = opts.findTextarea();
                if (!textarea) {
                    setTimeout(opts.retry, 500);
                    return;
                }

                // 转谱页：有内容时隐藏容器内的居中占位
                function hidePlaceholder() {
                    if (!opts.hidePlaceholderId) return;
                    var container = document.getElementById(opts.hidePlaceholderId);
                    if (!container) return;
                    var ph = container.querySelector('div[style*="text-align:center"]');
                    if (ph) ph.style.display = 'none';
                }

                function render() {
                    var current = opts.findTextarea();
                    var abcText = current ? (current.value || '') : '';
                    if (abcText && abcText.trim()) {
                        if (opts.ensureAccordion) ensureAbcAccordionOpen();
                        hidePlaceholder();
                        try {
                            ABCJS.renderAbc(opts.paperId, abcText, {
                                responsive: 'resize',
                                scale: 0.7,
                                staffwidth: 600
                            });
                            ABCJS.renderAudio(opts.audioId, abcText, {
                                displayLoop: true,
                                displayRestart: true,
                                displayPlay: true,
                                displayProgress: true
                            });
                        } catch (e) {
                            console.log((opts.errorLabel || 'ABC') + ' render error:', e);
                        }
                    } else if (opts.clearWhenEmpty) {
                        if (paper) paper.innerHTML = '';
                        var audioEl = document.getElementById(opts.audioId);
                        if (audioEl) audioEl.innerHTML = '';
                    }
                }

                var debounceTimer;
                function debouncedRender() {
                    clearTimeout(debounceTimer);
                    debounceTimer = setTimeout(render, 500);
                }

                // 生成页：用户直接编辑 ABC 文本时即时（防抖）渲染
                if (opts.liveInput) {
                    textarea.addEventListener('input', debouncedRender);
                }

                // 轮询文本变化（覆盖程序化设值场景，如历史页/转谱页回填）
                var lastValue = textarea.value;
                setInterval(function() {
                    var current = opts.findTextarea();
                    if (!current) return;
                    var currentValue = current.value || '';
                    if (currentValue !== lastValue) {
                        lastValue = currentValue;
                        if (opts.liveInput) {
                            if (currentValue && currentValue.trim() && opts.ensureAccordion) ensureAbcAccordionOpen();
                            debouncedRender();
                        } else {
                            render();
                        }
                    }
                }, 300);

                render();

                if (opts.bindExports) {
                    bindAbcExportButtons(opts.findTextarea, opts.paperId);
                }
            }

            // 生成页 ABC 预览（支持导出 MIDI/PNG，编辑即时渲染）
            function initAbcPreview() {
                initAbcPreviewCore({
                    retry: initAbcPreview,
                    paperId: 'abc-paper',
                    audioId: 'abc-audio',
                    findTextarea: function() { return findAbcTextareaById('gen-abc-output'); },
                    ensureAccordion: true,
                    liveInput: true,
                    bindExports: true,
                    errorLabel: 'ABC',
                });
            }
            initAbcPreview();

            // 历史页 ABC 预览（空乐谱时清空容器）
            function initHistoryAbcPreview() {
                initAbcPreviewCore({
                    retry: initHistoryAbcPreview,
                    paperId: 'history-abc-paper',
                    audioId: 'history-abc-audio',
                    findTextarea: function() { return findAbcTextareaById('history-abc'); },
                    requirePaper: true,
                    clearWhenEmpty: true,
                    errorLabel: 'History ABC',
                });
            }
            initHistoryAbcPreview();

            // 转谱页 ABC 预览（有内容时隐藏居中占位）
            function initTranscribeAbcPreview() {
                initAbcPreviewCore({
                    retry: initTranscribeAbcPreview,
                    paperId: 'transcribe-abc-paper',
                    audioId: 'transcribe-abc-audio',
                    findTextarea: function() {
                        var tas = document.querySelectorAll('textarea[placeholder*="转谱完成后"]');
                        return tas.length > 0 ? tas[0] : null;
                    },
                    requirePaper: true,
                    clearWhenEmpty: true,
                    hidePlaceholderId: 'transcribe-abc-preview-container',
                    errorLabel: 'Transcribe ABC',
                });
            }
            initTranscribeAbcPreview();

            function initAbcBridge() {
                var bridge = document.getElementById('abc-bridge');
                if (!bridge) {
                    setTimeout(initAbcBridge, 500);
                    return;
                }

                var style = document.createElement('style');
                style.textContent = '#abc-bridge { display: none !important; }';
                document.head.appendChild(style);

                var bridgeInput = bridge.querySelector('textarea') || bridge.querySelector('input[type="text"]') || bridge;

                function findGenAbcTextarea() {
                    var el = document.getElementById('gen-abc-output');
                    if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) return el;
                    if (el) {
                        var tab = el.querySelector('textarea, input[type="text"]');
                        if (tab) return tab;
                    }
                    return document.querySelector('#gen-abc-output textarea, #gen-abc-output input[type="text"]');
                }

                function clickTab(tabName) {
                    var tabs = document.querySelectorAll('button[role="tab"]');
                    for (var i = 0; i < tabs.length; i++) {
                        if (tabs[i].textContent.trim().indexOf(tabName) !== -1) {
                            tabs[i].click();
                            return;
                        }
                    }
                }

                var lastBridgeValue = bridgeInput.value;
                setInterval(function() {
                    var currentValue = bridgeInput.value || '';
                    if (currentValue !== lastBridgeValue && currentValue.trim()) {
                        lastBridgeValue = currentValue;

                        var genAbc = findGenAbcTextarea();
                        if (genAbc) {
                            var nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                            nativeSetter.call(genAbc, currentValue);
                            genAbc.dispatchEvent(new Event('input', { bubbles: true }));
                            genAbc.dispatchEvent(new Event('change', { bubbles: true }));
                        }

                        clickTab('创作');
                        bridgeInput.value = '';
                        lastBridgeValue = '';
                    }
                }, 200);

                console.log('ABC bridge initialized');
            }
            initAbcBridge();

            function initHistoryLyricSync() {
                var syncContainer = document.getElementById('history-lyric-sync');
                if (!syncContainer) { setTimeout(initHistoryLyricSync, 500); return; }

                var lyricsDataDiv = document.querySelector('.history-lyrics-data');
                if (!lyricsDataDiv) { setTimeout(initHistoryLyricSync, 500); return; }

                function renderLyrics() {
                    var rawLyrics = lyricsDataDiv.getAttribute('data-lyrics') || '';
                    var duration = parseFloat(lyricsDataDiv.getAttribute('data-duration')) || 0;
                    if (!rawLyrics) {
                        syncContainer.innerHTML = '<div style="color:#888;">无歌词数据</div>';
                        return;
                    }
                    var lyrics;
                    try { lyrics = JSON.parse(rawLyrics); } catch(e) { lyrics = rawLyrics; }

                    var segments = [];
                    var current = { name: '', lines: [] };
                    var lines = lyrics.split('\n');
                    for (var i = 0; i < lines.length; i++) {
                        var trimmed = lines[i].trim();
                        if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
                            if (current.name || current.lines.length > 0) segments.push(current);
                            current = { name: trimmed.slice(1, -1), lines: [] };
                        } else if (trimmed) {
                            current.lines.push(trimmed);
                        }
                    }
                    if (current.lines.length > 0) segments.push(current);
                    if (segments.length === 0 && lyrics.trim()) {
                        segments = [{ name: '', lines: lines.filter(function(l) { return l.trim(); }) }];
                    }

                    var totalLines = 0;
                    segments.forEach(function(s) { totalLines += s.lines.length; });
                    var timePerLine = totalLines > 0 && duration > 0 ? duration / totalLines : 4;

                    var html = '';
                    var lineIdx = 0;
                    for (var si = 0; si < segments.length; si++) {
                        var seg = segments[si];
                        if (seg.name) {
                            html += '<div style="font-weight:bold;color:#888;font-size:11px;margin-top:12px;text-transform:uppercase;letter-spacing:1px;">' + seg.name + '</div>';
                        }
                        for (var li = 0; li < seg.lines.length; li++) {
                            html += '<div class="lyric-line" data-start="' + (lineIdx * timePerLine).toFixed(2) + '" data-end="' + ((lineIdx + 1) * timePerLine).toFixed(2) + '" style="padding:3px 8px;margin:2px 0;border-radius:4px;font-size:15px;color:#999;transition:all 0.3s;">' + seg.lines[li] + '</div>';
                            lineIdx++;
                        }
                    }
                    syncContainer.innerHTML = html;

                    var allLines = syncContainer.querySelectorAll('.lyric-line');
                    function highlight() {
                        var audio = document.querySelector('audio');
                        if (!audio || audio.paused) return;
                        var t = audio.currentTime;
                        for (var i = 0; i < allLines.length; i++) {
                            var start = parseFloat(allLines[i].getAttribute('data-start'));
                            var end = parseFloat(allLines[i].getAttribute('data-end'));
                            if (t >= start && t < end) {
                                if (!allLines[i].classList.contains('active')) {
                                    allLines.forEach(function(l) { l.style.color = '#999'; l.style.background = 'transparent'; l.style.fontWeight = 'normal'; l.classList.remove('active'); });
                                    allLines[i].style.color = 'var(--body-text-color, #fff)';
                                    allLines[i].style.background = 'rgba(59,130,246,0.3)';
                                    allLines[i].style.fontWeight = 'bold';
                                    allLines[i].classList.add('active');
                                    allLines[i].scrollIntoView({ behavior: 'smooth', block: 'center' });
                                }
                                return;
                            }
                        }
                    }
                    document.querySelectorAll('audio').forEach(function(a) {
                        a.addEventListener('timeupdate', highlight);
                        a.addEventListener('play', highlight);
                    });
                }

                renderLyrics();

                var lastLyricsData = lyricsDataDiv.getAttribute('data-lyrics');
                setInterval(function() {
                    var div = document.querySelector('.history-lyrics-data');
                    if (!div) return;
                    var current = div.getAttribute('data-lyrics') || '';
                    if (current !== lastLyricsData) {
                        lastLyricsData = current;
                        lyricsDataDiv = div;
                        renderLyrics();
                    }
                }, 500);
            }
            initHistoryLyricSync();

            function initGenLyricSync() {
                var dataDiv = document.querySelector('.gen-lyrics-data');
                if (!dataDiv) { setTimeout(initGenLyricSync, 1000); return; }

                var audio = document.querySelector('audio');
                if (!audio) { setTimeout(initGenLyricSync, 1000); return; }

                audio.addEventListener('timeupdate', function() {
                    var div = document.querySelector('.gen-lyrics-data');
                    if (!div) return;
                    var rawLyrics = div.getAttribute('data-lyrics') || '';
                    var duration = parseFloat(div.getAttribute('data-duration')) || 0;
                    if (!rawLyrics || !duration) return;

                    var lyrics;
                    try { lyrics = JSON.parse(rawLyrics); } catch(e) { lyrics = rawLyrics; }

                    var segments = [];
                    var current = { name: '', lines: [] };
                    var lines = lyrics.split('\n');
                    for (var i = 0; i < lines.length; i++) {
                        var trimmed = lines[i].trim();
                        if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
                            if (current.name || current.lines.length > 0) segments.push(current);
                            current = { name: trimmed.slice(1, -1), lines: [] };
                        } else if (trimmed) {
                            current.lines.push(trimmed);
                        }
                    }
                    if (current.lines.length > 0) segments.push(current);

                    var totalLines = 0;
                    segments.forEach(function(s) { totalLines += s.lines.length; });
                    var timePerLine = totalLines > 0 ? duration / totalLines : 4;

                    var t = audio.currentTime;
                    var lineIdx = 0;
                    for (var si = 0; si < segments.length; si++) {
                        for (var li = 0; li < segments[si].lines.length; li++) {
                            var start = lineIdx * timePerLine;
                            var end = (lineIdx + 1) * timePerLine;
                            if (t >= start && t < end) {
                                var targetId = 'gen-lyric-' + lineIdx;
                                var el = document.getElementById(targetId);
                                if (el && !el.classList.contains('active')) {
                                    document.querySelectorAll('.gen-lyric-line.active').forEach(function(l) { l.style.color = '#999'; l.style.background = 'transparent'; l.style.fontWeight = 'normal'; l.classList.remove('active'); });
                                    el.style.color = 'var(--body-text-color, #fff)';
                                    el.style.background = 'rgba(59,130,246,0.3)';
                                    el.style.fontWeight = 'bold';
                                    el.classList.add('active');
                                    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                }
                                return;
                            }
                            lineIdx++;
                        }
                    }
                });
            }
            initGenLyricSync();

            function initAudioTimeDisplay() {
                // 覆盖所有波形播放器：主播放器（gen/history）+ 音色工坊播放器组
                // （分离/翻唱产出 sep-audio-*/cover-audio-* 与任务历史回放 sep/cover-history-audio-*）
                // 多个选择器必须先用 :is() 包成一个整体再接后代选择器：
                // 直接写 "a, b, c .timestamps" 时逗号列表只有最后一项带后代限定，
                // 前几项会命中播放器根节点，把整块播放器染成半透明黑（亮色主题下整条发黑）。
                // 固定 id 的播放器必须与下方 initPlayerZoom 的 PLAYER_IDS 保持一致，
                // 漏掉的会保留 Gradio 的 3px 近黑内联边框。
                var PLAYERS = '#gen-audio, #history-audio, ' +
                    '#lib-stem-preview, #lib-ref-preview, #cover-ref-preview, #cover-acc-preview, ' +
                    '[id^="sep-audio-"], [id^="cover-audio-"], [id^="sep-history-audio-"], [id^="cover-history-audio-"]';
                var SEL = ':is(' + PLAYERS + ')';
                var style = document.createElement('style');
                style.textContent = SEL + ' { overflow: visible !important; }' +
                    SEL + ' .component-wrapper { overflow: visible !important; }' +
                    SEL + ' .waveform-container { overflow: visible !important; }' +
                    // 播放器外框：Gradio 给音频块写了内联 border-style: solid 却没给宽度，
                    // 于是回落到默认 medium(3px) + currentColor —— 亮色主题下就是一圈近黑边框，
                    // 看着像"整块播放器是暗色的"。统一改回 Gradio 常规块边框（1px 主题边框色）。
                    SEL + ' { border: 1px solid var(--border-color-primary, transparent) !important; }' +
                    // 时间码条：底色/文字/描边改用 Gradio 主题变量，明暗主题自动适配
                    SEL + ' .timestamps { visibility: visible !important; opacity: 1 !important; font-size: 15px !important; font-weight: bold !important; color: var(--body-text-color, #fff) !important; font-family: monospace !important; letter-spacing: 0.5px !important; padding: 4px 12px !important; background: var(--background-fill-secondary, rgba(0,0,0,0.7)) !important; border: 1px solid var(--border-color-primary, transparent) !important; border-radius: 4px !important; display: flex !important; justify-content: space-between !important; align-items: center !important; margin-top: 12px !important; width: 100% !important; box-sizing: border-box !important; }' +
                    // 当前时间读数：亮色用深绿保证对比度，暗色用亮绿
                    SEL + ' .timestamps time { color: #15803d !important; font-size: 15px !important; }' +
                    'body.dark ' + SEL + ' .timestamps time { color: #4ade80 !important; font-size: 15px !important; }';
                document.head.appendChild(style);
            }
            initAudioTimeDisplay();

            function initPlayerZoom() {
                if (typeof WaveSurfer === 'undefined') {
                    console.warn('PlayerZoom: wavesurfer not loaded, zoom disabled');
                    return;
                }

                // 主播放器 + 音色工坊播放器组（固定槽位按 elem_id 前缀收集，
                // 组件初始隐藏/按需显隐，由 watchPlayer 轮询接管）
                // 另含不带前缀的散装播放器：分离/翻唱页各处「试听」播放器
                // （lib-*/cover-*-preview）
                var PLAYER_IDS = ['gen-audio', 'history-audio',
                                  'lib-stem-preview', 'lib-ref-preview',
                                  'cover-ref-preview', 'cover-acc-preview'];
                ['sep-audio-', 'cover-audio-', 'sep-history-audio-', 'cover-history-audio-'].forEach(function(prefix) {
                    for (var i = 0; i < 6; i++) PLAYER_IDS.push(prefix + i);
                });
                var STEPS = [0, 1, 2, 5, 10, 20, 40, 80, 160];
                var DEFAULT_STEP = 0;

                var style = document.createElement('style');
                style.textContent =
                    '.yz-wave-wrap { margin-top: 10px; }' +
                    '.yz-toolbar { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }' +
                    // 工具条按钮/波形底改用 Gradio 主题变量，明暗主题自动适配
                    '.yz-toolbar button { background: var(--button-secondary-background-fill, rgba(0,0,0,0.5)); color: var(--button-secondary-text-color, #fff); border: 1px solid var(--border-color-primary, rgba(255,255,255,0.25)); border-radius: 4px; padding: 3px 12px; cursor: pointer; font-size: 13px; line-height: 1.5; }' +
                    '.yz-toolbar button:hover { border-color: var(--color-accent, #3b82f6); }' +
                    '.yz-toolbar button:disabled { opacity: 0.4; cursor: default; }' +
                    '.yz-zoom-label { color: #15803d; font-size: 12px; font-family: monospace; margin-left: 6px; min-width: 70px; }' +
                    'body.dark .yz-zoom-label { color: #4ade80; }' +
                    '.yz-wave { height: 80px; border-radius: 4px; background: var(--background-fill-secondary, rgba(0,0,0,0.25)); }' +
                    // 等待提示：Gradio 要先整文件下完才给播放器 src（远端隧道下大件要几十秒），
                    // 期间播放器是 0:00 空壳，需明确提示"正在加载"避免看起来像坏了
                    '.yz-loading { margin-top: 8px; padding: 6px 10px; font-size: 13px; line-height: 1.4; border-radius: 4px; color: var(--body-text-color, #333); background: var(--background-fill-secondary, rgba(0,0,0,0.06)); border: 1px solid var(--border-color-primary, rgba(0,0,0,0.15)); }' +
                    '.yz-hidden { display: none !important; }';
                document.head.appendChild(style);

                function getShadowAudio(root) {
                    var wf = root.querySelector('#waveform');
                    if (!wf) return null;
                    var host = wf.firstElementChild;
                    if (!host || !host.shadowRoot) return null;
                    var audio = host.shadowRoot.querySelector('audio');
                    if (audio && audio.getAttribute('src')) return audio;
                    return null;
                }

                function watchPlayer(rootId) {
                    var st = null; // active instance state
                    var mutating = false;
                    var boundRoot = null; // 当前绑定的组件根（Gradio 显隐切换会销毁重建 DOM，需重绑）

                    function teardown() {
                        if (!st) return;
                        if (st.loadstartHandler && st.audioEl) {
                            st.audioEl.removeEventListener('loadstart', st.loadstartHandler);
                        }
                        if (st.ws) { try { st.ws.destroy(); } catch (e) {} }
                        if (st.wrapEl && st.wrapEl.parentNode) {
                            mutating = true;
                            st.wrapEl.parentNode.removeChild(st.wrapEl);
                            mutating = false;
                        }
                        if (boundRoot) {
                            var orig = boundRoot.querySelector('.waveform-container');
                            if (orig) orig.classList.remove('yz-hidden');
                        }
                        st = null;
                    }

                    function applyZoom() {
                        if (!st || !st.ws) return;
                        var px = STEPS[st.stepIdx];
                        st.ws.zoom(px);
                        if (st.labelEl) {
                            st.labelEl.textContent = px === 0 ? '适应宽度' : (px + ' px/s');
                        }
                        if (st.btnOut) st.btnOut.disabled = (st.stepIdx === 0);
                        if (st.btnIn) st.btnIn.disabled = (st.stepIdx === STEPS.length - 1);
                    }

                    function tryInit() {
                        if (st || !boundRoot) return;
                        var container = boundRoot.querySelector('.waveform-container');
                        var audioEl = getShadowAudio(boundRoot);
                        if (!container || !audioEl) return;

                        st = { ws: null, wrapEl: null, waveEl: null, labelEl: null,
                               btnIn: null, btnOut: null, audioEl: audioEl,
                               stepIdx: DEFAULT_STEP, loadstartHandler: null };

                        mutating = true;

                        var wrap = document.createElement('div');
                        wrap.className = 'yz-wave-wrap';

                        var toolbar = document.createElement('div');
                        toolbar.className = 'yz-toolbar';

                        var btnOut = document.createElement('button');
                        btnOut.textContent = '−';
                        btnOut.title = '缩小';
                        btnOut.addEventListener('click', function() {
                            if (st && st.stepIdx > 0) { st.stepIdx--; applyZoom(); }
                        });

                        var btnFit = document.createElement('button');
                        btnFit.textContent = '适应宽度';
                        btnFit.title = '整首歌铺满，无横向滚动条';
                        btnFit.addEventListener('click', function() {
                            if (st) { st.stepIdx = 0; applyZoom(); }
                        });

                        var btnIn = document.createElement('button');
                        btnIn.textContent = '+';
                        btnIn.title = '放大';
                        btnIn.addEventListener('click', function() {
                            if (st && st.stepIdx < STEPS.length - 1) { st.stepIdx++; applyZoom(); }
                        });

                        var label = document.createElement('span');
                        label.className = 'yz-zoom-label';
                        label.textContent = '适应宽度';

                        toolbar.appendChild(btnOut);
                        toolbar.appendChild(btnFit);
                        toolbar.appendChild(btnIn);
                        toolbar.appendChild(label);

                        var wave = document.createElement('div');
                        wave.className = 'yz-wave';

                        wrap.appendChild(toolbar);
                        wrap.appendChild(wave);
                        container.parentNode.insertBefore(wrap, container);

                        st.wrapEl = wrap;
                        st.waveEl = wave;
                        st.labelEl = label;
                        st.btnIn = btnIn;
                        st.btnOut = btnOut;

                        // 波形配色随明暗主题切换：亮色主题下白色光标落在白底上完全不可见，
                        // 亮绿进度色与白底对比度也不足，需改用深色系。
                        var isDark = document.body.classList.contains('dark');
                        try {
                            st.ws = WaveSurfer.create({
                                container: wave,
                                media: audioEl,
                                height: 80,
                                waveColor: isDark ? '#7f7f7f' : '#a1a1aa',
                                progressColor: isDark ? '#4ade80' : '#15803d',
                                cursorColor: isDark ? '#ffffff' : '#1f2328',
                                cursorWidth: 1
                            });
                        } catch (e) {
                            console.warn('PlayerZoom: create failed', e);
                            teardown();
                            mutating = false;
                            return;
                        }

                        st.ws.on('ready', function() {
                            var orig = boundRoot.querySelector('.waveform-container');
                            if (orig) orig.classList.add('yz-hidden');
                            applyZoom();
                        });
                        st.ws.on('error', function(e) {
                            console.warn('PlayerZoom: decode failed, falling back', e);
                            teardown();
                        });

                        st.loadstartHandler = function() {
                            // src changed on the same element: rebuild for new track
                            setTimeout(function() { teardown(); tryInit(); }, 0);
                        };
                        audioEl.addEventListener('loadstart', st.loadstartHandler);

                        mutating = false;
                    }

                    var observer = new MutationObserver(function() {
                        if (mutating || !boundRoot) return;
                        var audioNow = getShadowAudio(boundRoot);
                        if (st) {
                            if (audioNow !== st.audioEl || !boundRoot.contains(st.wrapEl)) {
                                teardown();
                                tryInit();
                            }
                        } else {
                            tryInit();
                        }
                    });

                    // 加载提示：Gradio 先把整个文件下完才给播放器挂 src（远端经隧道时
                    // 单轨 WAV 60–100MB 要等几十秒），等待期播放器是 0:00 空壳。
                    // 这里在等待期挂一个"正在加载音频… Ns"浮层，src 就绪后自动移除。
                    // #waveform 只在组件已赋值时渲染（无值时只画空白占位 DIV.empty），
                    // 用它区分"空播放器"与"已赋值但 src 未就绪"，避免空播放器上误显示提示。
                    function hasPlayerChrome(root) {
                        return !!(root && root.querySelector('#waveform'));
                    }
                    var loadingEl = null;
                    var loadingT0 = 0;
                    function syncLoading() {
                        var root = boundRoot;
                        var need = !!root && root.offsetParent !== null &&
                            hasPlayerChrome(root) && !getShadowAudio(root);
                        if (!need) {
                            if (loadingEl) {
                                mutating = true;
                                if (loadingEl.parentNode) loadingEl.parentNode.removeChild(loadingEl);
                                loadingEl = null;
                                mutating = false;
                            }
                            return;
                        }
                        if (!loadingEl || !loadingEl.isConnected) {
                            loadingEl = document.createElement('div');
                            loadingEl.className = 'yz-loading';
                            loadingT0 = Date.now();
                            mutating = true;
                            root.appendChild(loadingEl);
                            mutating = false;
                        }
                        loadingEl.textContent = '正在加载音频… ' +
                            Math.round((Date.now() - loadingT0) / 1000) + 's';
                    }

                    // 轮询：组件根出现/销毁重建（Gradio 显隐切换）时重绑 observer 并重新初始化
                    function poll() {
                        var root = document.getElementById(rootId);
                        if (root !== boundRoot) {
                            observer.disconnect();
                            boundRoot = root;
                            if (st) teardown(); // 旧实例随旧 DOM 失效
                            if (boundRoot) {
                                observer.observe(boundRoot, { childList: true, subtree: true });
                                tryInit();
                            }
                        }
                        syncLoading();
                        setTimeout(poll, 1000);
                    }
                    poll();
                }

                PLAYER_IDS.forEach(watchPlayer);
            }
            initPlayerZoom();

            if (window.initHistoryTableClick) {
                window.initHistoryTableClick();
            }
        })();

        setTimeout(function() {
            if (window.initHistoryTableClick) {
                window.initHistoryTableClick();
            }
        }, 2000);