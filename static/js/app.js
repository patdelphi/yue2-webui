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

            var triggerInput = document.querySelector('#history-row-trigger input[type="number"]');
            if (!triggerInput) {
                var triggerEl = document.getElementById('history-row-trigger');
                if (!triggerEl) { setTimeout(window.initHistoryTableClick, 500); return; }
                triggerInput = triggerEl.querySelector('input') || triggerEl;
            }

            function setTriggerValue(index) {
                var input = triggerInput;
                if (input.tagName === 'DIV') {
                    input = input.querySelector('input') || input;
                }
                if (!input || input.value === undefined) return;
                var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeInputValueSetter.call(input, index);
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            }

            tableContainer.addEventListener('click', function(e) {
                var row = e.target.closest('tbody tr');
                if (!row) return;

                var tbody = row.parentElement;
                if (!tbody || tbody.tagName !== 'TBODY') return;

                var rows = Array.from(tbody.querySelectorAll('tr'));
                var index = rows.indexOf(row);
                if (index < 0) return;

                setTriggerValue(index);

                rows.forEach(function(r) { r.style.background = ''; });
                row.style.background = 'rgba(59,130,246,0.2)';
            });

            var style = document.createElement('style');
            style.textContent = '#history-table tbody tr { cursor: pointer; } #history-table tbody tr:hover { background: rgba(59,130,246,0.1) !important; } #history-row-trigger, #history-row-trigger + .block-info { display: none !important; }';
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
                        'background:rgba(255,255,255,0.05);border-radius:6px;margin:3px 0;' +
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
            }
            initLyricsEditor();

            function initAbcPreview() {
                if (typeof ABCJS === 'undefined') {
                    setTimeout(initAbcPreview, 500);
                    return;
                }

                function findAbcTextarea() {
                    var el = document.getElementById('gen-abc-output');
                    if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) return el;
                    if (el) {
                        var tab = el.querySelector('textarea, input[type="text"]');
                        if (tab) return tab;
                    }
                    return document.querySelector('#gen-abc-output textarea, #gen-abc-output input[type="text"]');
                }

                function ensureAccordionOpen(textarea) {
                    if (!textarea) return;
                    var buttons = document.querySelectorAll('button.label-wrap');
                    for (var i = 0; i < buttons.length; i++) {
                        var btn = buttons[i];
                        if (btn.textContent.includes('生成的乐谱')) {
                            if (!btn.classList.contains('open')) {
                                btn.click();
                            }
                            break;
                        }
                    }
                }

                var abcTextarea = findAbcTextarea();
                if (!abcTextarea) {
                    setTimeout(initAbcPreview, 500);
                    return;
                }

                function renderAbc() {
                    var current = findAbcTextarea();
                    var abcText = current ? current.value : '';
                    if (abcText && abcText.trim()) {
                        ensureAccordionOpen(current);
                        try {
                            ABCJS.renderAbc("abc-paper", abcText, {
                                responsive: "resize",
                                scale: 0.7,
                                staffwidth: 600
                            });
                            ABCJS.renderAudio("abc-audio", abcText, {
                                displayLoop: true,
                                displayRestart: true,
                                displayPlay: true,
                                displayProgress: true
                            });
                        } catch (e) {
                            console.log("ABC render error:", e);
                        }
                    }
                }

                let debounceTimer;
                function debouncedRender() {
                    clearTimeout(debounceTimer);
                    debounceTimer = setTimeout(renderAbc, 500);
                }

                abcTextarea.addEventListener('input', debouncedRender);

                var lastValue = abcTextarea.value;
                setInterval(function() {
                    var current = findAbcTextarea();
                    if (!current) return;
                    if (current.value !== lastValue) {
                        lastValue = current.value;
                        if (current.value && current.value.trim()) {
                            ensureAccordionOpen(current);
                        }
                        debouncedRender();
                    }
                }, 300);

                renderAbc();

                document.querySelectorAll('button').forEach(function(btn) {
                    if (btn.textContent.includes('\u5BFC\u51FA MIDI')) {
                        btn.onclick = function() {
                            var ta = findAbcTextarea();
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
                            const svg = document.querySelector('#abc-paper svg');
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
            initAbcPreview();

            function initHistoryAbcPreview() {
                if (typeof ABCJS === 'undefined') {
                    setTimeout(initHistoryAbcPreview, 500);
                    return;
                }

                var historyPaper = document.getElementById('history-abc-paper');
                var historyAudio = document.getElementById('history-abc-audio');
                if (!historyPaper) {
                    setTimeout(initHistoryAbcPreview, 500);
                    return;
                }

                function findHistoryAbcTextarea() {
                    var el = document.getElementById('history-abc');
                    if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) return el;
                    if (el) {
                        var tab = el.querySelector('textarea, input[type="text"]');
                        if (tab) return tab;
                    }
                    return document.querySelector('#history-abc textarea, #history-abc input[type="text"]');
                }

                var abcTextarea = findHistoryAbcTextarea();
                if (!abcTextarea) {
                    setTimeout(initHistoryAbcPreview, 500);
                    return;
                }

                function renderHistoryAbc() {
                    var current = findHistoryAbcTextarea();
                    var abcText = current ? (current.value || '') : '';
                    if (abcText && abcText.trim()) {
                        try {
                            ABCJS.renderAbc("history-abc-paper", abcText, {
                                responsive: "resize",
                                scale: 0.7,
                                staffwidth: 600
                            });
                            ABCJS.renderAudio("history-abc-audio", abcText, {
                                displayLoop: true,
                                displayRestart: true,
                                displayPlay: true,
                                displayProgress: true
                            });
                        } catch (e) {
                            console.log("History ABC render error:", e);
                        }
                    } else {
                        if (historyPaper) historyPaper.innerHTML = '';
                        if (historyAudio) historyAudio.innerHTML = '';
                    }
                }

                var lastHistoryValue = abcTextarea.value;
                setInterval(function() {
                    var current = findHistoryAbcTextarea();
                    var currentValue = current ? (current.value || '') : '';
                    if (currentValue !== lastHistoryValue) {
                        lastHistoryValue = currentValue;
                        renderHistoryAbc();
                    }
                }, 300);

                renderHistoryAbc();
            }
            initHistoryAbcPreview();

            function initTranscribeAbcPreview() {
                if (typeof ABCJS === 'undefined') {
                    setTimeout(initTranscribeAbcPreview, 500);
                    return;
                }

                var paper = document.getElementById('transcribe-abc-paper');
                var audio = document.getElementById('transcribe-abc-audio');
                if (!paper) {
                    setTimeout(initTranscribeAbcPreview, 500);
                    return;
                }

                function findTranscribeAbcTextarea() {
                    var textareas = document.querySelectorAll('textarea[placeholder*="转谱完成后"]');
                    return textareas.length > 0 ? textareas[0] : null;
                }

                var abcTextarea = findTranscribeAbcTextarea();
                if (!abcTextarea) {
                    setTimeout(initTranscribeAbcPreview, 500);
                    return;
                }

                function renderTranscribeAbc() {
                    var current = findTranscribeAbcTextarea();
                    var abcText = current ? (current.value || '') : '';
                    if (abcText && abcText.trim()) {
                        var container = document.getElementById('transcribe-abc-preview-container');
                        if (container) {
                            var placeholder = container.querySelector('div[style*="text-align:center"]');
                            if (placeholder) placeholder.style.display = 'none';
                        }
                        try {
                            ABCJS.renderAbc("transcribe-abc-paper", abcText, {
                                responsive: "resize",
                                scale: 0.7,
                                staffwidth: 600
                            });
                            ABCJS.renderAudio("transcribe-abc-audio", abcText, {
                                displayLoop: true,
                                displayRestart: true,
                                displayPlay: true,
                                displayProgress: true
                            });
                        } catch (e) {
                            console.log("Transcribe ABC render error:", e);
                        }
                    } else {
                        if (paper) paper.innerHTML = '';
                        if (audio) audio.innerHTML = '';
                    }
                }

                var lastValue = abcTextarea.value;
                setInterval(function() {
                    var current = findTranscribeAbcTextarea();
                    var currentValue = current ? (current.value || '') : '';
                    if (currentValue !== lastValue) {
                        lastValue = currentValue;
                        renderTranscribeAbc();
                    }
                }, 300);

                renderTranscribeAbc();
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
                                    allLines[i].style.color = '#fff';
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
                                    el.style.color = '#fff';
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
                var style = document.createElement('style');
                style.textContent = '#gen-audio, #history-audio { overflow: visible !important; }' +
                    '#gen-audio .component-wrapper, #history-audio .component-wrapper { overflow: visible !important; }' +
                    '#gen-audio .waveform-container, #history-audio .waveform-container { overflow: visible !important; }' +
                    '#gen-audio .timestamps, #history-audio .timestamps { visibility: visible !important; opacity: 1 !important; font-size: 15px !important; font-weight: bold !important; color: #fff !important; font-family: monospace !important; letter-spacing: 0.5px !important; text-shadow: 0 2px 4px rgba(0,0,0,0.5) !important; padding: 4px 12px !important; background: rgba(0,0,0,0.7) !important; border-radius: 4px !important; display: flex !important; justify-content: space-between !important; align-items: center !important; margin-top: 12px !important; width: 100% !important; box-sizing: border-box !important; }' +
                    '#gen-audio .timestamps time, #history-audio .timestamps time { color: #4ade80 !important; font-size: 15px !important; }';
                document.head.appendChild(style);
            }
            initAudioTimeDisplay();

            function initPlayerZoom() {
                if (typeof WaveSurfer === 'undefined') {
                    console.warn('PlayerZoom: wavesurfer not loaded, zoom disabled');
                    return;
                }

                var PLAYER_IDS = ['gen-audio', 'history-audio'];
                var STEPS = [0, 1, 2, 5, 10, 20, 40, 80, 160];
                var DEFAULT_STEP = 0;

                var style = document.createElement('style');
                style.textContent =
                    '.yz-wave-wrap { margin-top: 10px; }' +
                    '.yz-toolbar { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }' +
                    '.yz-toolbar button { background: rgba(0,0,0,0.5); color: #fff; border: 1px solid rgba(255,255,255,0.25); border-radius: 4px; padding: 3px 12px; cursor: pointer; font-size: 13px; line-height: 1.5; }' +
                    '.yz-toolbar button:hover { background: rgba(255,255,255,0.15); }' +
                    '.yz-toolbar button:disabled { opacity: 0.4; cursor: default; }' +
                    '.yz-zoom-label { color: #4ade80; font-size: 12px; font-family: monospace; margin-left: 6px; min-width: 70px; }' +
                    '.yz-wave { height: 80px; border-radius: 4px; background: rgba(0,0,0,0.25); }' +
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
                    var root = document.getElementById(rootId);
                    if (!root) {
                        setTimeout(function() { watchPlayer(rootId); }, 1000);
                        return;
                    }

                    var st = null; // active instance state
                    var mutating = false;

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
                        var orig = root.querySelector('.waveform-container');
                        if (orig) orig.classList.remove('yz-hidden');
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
                        if (st) return;
                        var container = root.querySelector('.waveform-container');
                        var audioEl = getShadowAudio(root);
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

                        try {
                            st.ws = WaveSurfer.create({
                                container: wave,
                                media: audioEl,
                                height: 80,
                                waveColor: '#7f7f7f',
                                progressColor: '#4ade80',
                                cursorColor: '#ffffff',
                                cursorWidth: 1
                            });
                        } catch (e) {
                            console.warn('PlayerZoom: create failed', e);
                            teardown();
                            mutating = false;
                            return;
                        }

                        st.ws.on('ready', function() {
                            var orig = root.querySelector('.waveform-container');
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
                        if (mutating) return;
                        var audioNow = getShadowAudio(root);
                        if (st) {
                            if (audioNow !== st.audioEl || !root.contains(st.wrapEl)) {
                                teardown();
                                tryInit();
                            }
                        } else {
                            tryInit();
                        }
                    });
                    observer.observe(root, { childList: true, subtree: true });

                    tryInit();
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