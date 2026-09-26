(() => {
  const catalogUrl = '/edition/catalog.json';
  const editionPattern = /^\/(-?\d{3})\/?$/;
  let activeCatalog;

  const escapeHtml = (value) => String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');

  const hebrewFirst = (value) => String(value)
    .replaceAll('—', ' · ')
    .replaceAll('Meta Newsroom', 'חדר החדשות של מטא')
    .replaceAll('TechCrunch', 'טק־קראנץ׳')
    .replaceAll('GitHub Changelog', 'יומן העדכונים של GitHub')
    .replaceAll('changelog', 'יומן עדכונים')
    .replaceAll('Medicare', 'מדיקר')
    .replaceAll('Copilot', 'קופיילוט')
    .replaceAll('Muse', 'מיוז')
    .replaceAll('Meta', 'מטא');

  const renderParagraphs = (value) => hebrewFirst(value)
    .split(/\n\s*\n/)
    .filter(Boolean)
    .map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`)
    .join('');

  function requestedEdition(catalog) {
    const match = window.location.pathname.match(editionPattern);
    return match ? match[1] : catalog.latest;
  }

  function formatDate(date) {
    if (!date || typeof date !== 'string') return 'לא ידוע';
    const [year, month, day] = date.split('-');
    return year && month && day ? `${day}.${month}.${year}` : date;
  }

  function formatDateTime(value) {
    if (!value || typeof value !== 'string') return 'לא ידוע';
    const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    return match ? `${match[3]}.${match[2]}.${match[1]} · ${match[4]}:${match[5]}` : value;
  }

  function sourceLabel(source) {
    if (source.label || source.displayName || source.name || source.publisher) {
      return String(source.label || source.displayName || source.name || source.publisher).replaceAll('—', ' · ');
    }
    try { return new URL(source.url).hostname.replace(/^www\./, ''); } catch { return 'מקור'; }
  }

  function editionLabel(edition) {
    return edition.editionType === 'weekly' ? 'מהדורה שבועית' : 'מהדורה';
  }


  function editionSections(edition) {
    if (Array.isArray(edition.sections)) return edition.sections;
    return null;
  }

  function editionStories(edition) {
    const sections = editionSections(edition);
    if (sections) return sections.flatMap((section) => Array.isArray(section.stories) ? section.stories : []);
    return Array.isArray(edition.stories) ? edition.stories : [];
  }

  function renderSource(source, index, story) {
    const label = String(sourceLabel(source));
    const link = `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer" dir="auto" aria-label="${escapeHtml(label)} · מקור ${index + 1} לסיפור ${escapeHtml(hebrewFirst(story.headline || ''))} (נפתח בלשונית חדשה)"><bdi dir="auto">${escapeHtml(label)}</bdi></a>`;
    return `<li class="source-item"><span class="source-number">${index + 1}.</span>${link}</li>`;
  }

  function renderSourceList(story) {
    const sources = Array.isArray(story.sources) ? story.sources : [];
    if (!sources.length) return '';
    return `<details class="source-links"><summary>מקורות <span>(${sources.length})</span></summary><ol>${sources.map((source, index) => renderSource(source, index, story)).join('')}</ol></details>`;
  }

  function renderLegacyStory(story, index) {
    const number = String(index + 1).padStart(2, '0');
    const sources = story.sources.map((source, sourceIndex) =>
      `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer" aria-label="מקור ${sourceIndex + 1} לכתבה בנושא ${escapeHtml(story.section)} (נפתח בלשונית חדשה)">${story.sources.length === 1 ? 'לכתבה המלאה' : `מקור ${sourceIndex + 1}`} ↗</a>`
    ).join('');
    return `<section class="story">
      <aside class="story-context"><p class="eyebrow">הקשר</p><p>${escapeHtml(hebrewFirst(story.whyItMatters))}</p><div class="source-links"><p class="eyebrow">מקורות</p>${sources}</div></aside>
      <div class="story-main"><p class="eyebrow story-label"><span>${escapeHtml(hebrewFirst(story.section))}</span></p><h2>${escapeHtml(hebrewFirst(story.headline))}</h2><p>${escapeHtml(hebrewFirst(story.summary))}</p></div>
      <div class="story-index"><b>${number}</b><span>${escapeHtml(story.readingTime)}</span></div>
    </section>`;
  }

  function renderWeeklyStory(story, index, sectionHeading = '') {
    const number = String(index + 1).padStart(2, '0');
    const section = story.section || sectionHeading;
    const established = Array.isArray(story.established) && story.established.length
      ? `<section class="story-depth"><h3>${story.editorialTrack ? 'הרקע' : 'מה ידוע'}</h3><ul>${story.established.map((item) => `<li>${escapeHtml(hebrewFirst(item))}</li>`).join('')}</ul></section>` : '';
    const uncertainties = Array.isArray(story.uncertainties) && story.uncertainties.length
      ? `<section class="story-depth"><h3>${story.editorialTrack ? 'מה נבדוק בהמשך' : 'מה עדיין לא ידוע'}</h3><ul>${story.uncertainties.map((item) => `<li>${escapeHtml(hebrewFirst(item))}</li>`).join('')}</ul></section>` : '';
    const context = [story.whyItMatters, story.editorialAssessment].filter(Boolean).map((item) => `<p>${escapeHtml(hebrewFirst(item))}</p>`).join('');
    return `<section class="story${story.editorialTrack ? ' editorial-track' : ''}">
      <aside class="story-context"><p class="eyebrow">הקשר</p>${context}${renderSourceList(story)}</aside>
      <div class="story-main"><p class="eyebrow story-label"><span>${escapeHtml(hebrewFirst(section))}</span></p><h2>${escapeHtml(hebrewFirst(story.headline))}</h2>${renderParagraphs(story.summary)}${established}${uncertainties}</div>
      <div class="story-index"><b>${number}</b><span>${escapeHtml(story.readingTime)}</span></div>
    </section>`;
  }

  function renderStories(edition, stories) {
    const sections = editionSections(edition);
    if (!sections) return stories.map(renderLegacyStory).join('');
    let index = 0;
    return sections.map((section) => {
      const sectionStories = Array.isArray(section.stories) ? section.stories : [];
      const rendered = sectionStories.map((story) => renderWeeklyStory(story, index++, section.heading || '')).join('');
      const description = section.description ? `<p>${escapeHtml(hebrewFirst(section.description))}</p>` : '';
      const empty = sectionStories.length ? '' : '<p class="empty-section">אין השבוע עדכוני המשך מהותיים.</p>';
      return `<section class="story-section${sectionStories.length ? '' : ' is-empty'}" data-section-id="${escapeHtml(section.id || '')}"><header class="story-section-header"><h3>${escapeHtml(hebrewFirst(section.heading || ''))}</h3>${description}</header>${rendered}${empty}</section>`;
    }).join('');
  }

  function renderEdition(edition, catalog) {
    const formattedDate = formatDate(edition.publicationDate);
    const modifiedDate = edition.dateModified ? formatDateTime(edition.dateModified) : null;
    const stories = editionStories(edition);
    const label = editionLabel(edition);
    document.title = `AI היום | ${label} ${edition.number}`;
    const canonical = `${window.location.origin}/${edition.number}`;
    document.querySelector('link[rel="canonical"]').href = canonical;
    document.querySelector('meta[property="og:title"]').content = document.title;
    document.querySelector('meta[property="og:url"]').content = canonical;
    document.querySelector('meta[name="twitter:title"]').content = document.title;
    document.querySelector('meta[name="description"]').content = hebrewFirst(`${edition.headline} · ${edition.introduction}`);
    document.querySelector('meta[property="og:description"]').content = edition.editionType === 'weekly'
      ? hebrewFirst(`${edition.headline} · תדריך שבועי של חמש דקות על AI בעברית.`)
      : hebrewFirst(`${edition.headline} · חמש דקות של חדשות AI בעברית.`);
    document.querySelector('.metadata .edition-label').innerHTML = `${label} <bdi>${escapeHtml(edition.number)}</bdi>`;
    const time = document.querySelector('.metadata time');
    time.dateTime = edition.publicationDate;
    time.textContent = formattedDate;

    const inside = document.querySelector('[data-edition-root]');
    inside.setAttribute('aria-label', `תוכן מהדורה ${edition.number}`);
    const header = inside.querySelector('.inside-header');
    const coverage = edition.coverageStart && edition.coverageEnd
      ? `<p class="coverage"><span>חלון הסיקור: <bdi>${formatDate(edition.coverageStart)}–${formatDate(edition.coverageEnd)}</bdi></span> <span>פורסם <time datetime="${escapeHtml(edition.publicationDate)}">${formattedDate}</time></span>${modifiedDate ? ` <span>עודכן <time datetime="${escapeHtml(edition.dateModified)}">${escapeHtml(modifiedDate)}</time></span>` : ''}</p>`
      : '';
    const archiveNote = edition.editionType === 'weekly'
      ? '<p class="archive-note">המהדורות הזמינות מופיעות בארכיון בתחתית העמוד.</p>'
      : '';
    header.innerHTML = `${coverage}<h2>${escapeHtml(hebrewFirst(edition.headline))}</h2><p>${escapeHtml(hebrewFirst(edition.introduction))}</p>${archiveNote}${edition.editorialNote ? `<p class="draft-note">${escapeHtml(hebrewFirst(edition.editorialNote))}</p>` : ''}<p class="ai-disclosure">${escapeHtml(hebrewFirst(edition.aiDisclosure))}</p>`;
    const quickRead = inside.querySelector('.quick-read');
    if (Array.isArray(edition.overview) && edition.overview.length) {
      quickRead.hidden = false;
      quickRead.innerHTML = `<h3>במבט אחד · חמשת הנושאים</h3><ul>${edition.overview.map((item) => `<li><strong>${escapeHtml(hebrewFirst(item.title))}:</strong> ${escapeHtml(hebrewFirst(item.summary))}</li>`).join('')}</ul>`;
    } else {
      quickRead.hidden = edition.editionType === 'weekly';
    }
    if (!quickRead.hidden && !(Array.isArray(edition.overview) && edition.overview.length)) {
      const storyCountLabels = { 4: 'ארבעת', 5: 'חמשת', 6: 'ששת' };
      const storyCountLabel = storyCountLabels[stories.length] || 'מספר';
      quickRead.innerHTML = `<h3>במבט אחד · ${storyCountLabel} הנושאים</h3><ul>${stories.map((story) => `<li><strong>${escapeHtml(hebrewFirst(story.section || ''))}:</strong> ${escapeHtml(hebrewFirst(story.quickRead))}</li>`).join('')}</ul>`;
    }
    inside.querySelector('.stories').innerHTML = renderStories(edition, stories);
    inside.querySelector('.takeaway').innerHTML = `<p class="eyebrow">${escapeHtml(edition.totalReadingTime)} · השורה התחתונה</p><h2>${escapeHtml(hebrewFirst(edition.takeaway))}</h2>`;

    renderArchive(catalog, edition.number);
  }

  async function renderArchive(catalog, currentNumber) {
    const archive = document.querySelector('.edition-archive');
    const numbers = catalog.editions.filter((number) => number !== currentNumber).reverse();
    const entries = await Promise.all(numbers.map(async (number) => {
      try {
        const response = await fetch(`/edition/${number}/edition.json`, { cache: 'no-store' });
        if (!response.ok) return null;
        const item = await response.json();
        return `<li><a href="/${escapeHtml(number)}"><span><b>מהדורה ${escapeHtml(number)}</b><time datetime="${escapeHtml(item.publicationDate)}">${formatDate(item.publicationDate)}</time></span><strong>${escapeHtml(hebrewFirst(item.headline))}</strong>${item.totalReadingTime ? `<small>${escapeHtml(item.totalReadingTime)} דקות קריאה</small>` : ''}</a></li>`;
      } catch { return null; }
    }));
    archive.innerHTML = `<h2>ארכיון המהדורות</h2>${entries.some(Boolean) ? `<ul>${entries.filter(Boolean).join('')}</ul>` : '<p>זוהי המהדורה הראשונה.</p>'}`;
  }

  function showError(message) {
    const error = document.createElement('p');
    error.className = 'load-error';
    error.setAttribute('role', 'alert');
    error.textContent = message;
    document.body.prepend(error);
  }

  async function load() {
    const catalogResponse = await fetch(catalogUrl, { cache: 'no-store' });
    if (!catalogResponse.ok) throw new Error('catalog');
    const catalog = await catalogResponse.json();
    activeCatalog = catalog;
    const number = requestedEdition(catalog);
    if (!catalog.editions.includes(number)) {
      const editionRoot = document.querySelector('[data-edition-root]');
      if (editionRoot) editionRoot.replaceChildren();
      document.title = 'AI היום | המהדורה אינה זמינה';
      showError('המהדורה המבוקשת אינה קיימת.');
      document.body.classList.add('ready');
      return;
    }
    const editionResponse = await fetch(`/edition/${number}/edition.json`);
    if (!editionResponse.ok) throw new Error('edition');
    renderEdition(await editionResponse.json(), catalog);
    document.body.classList.add('ready');
  }

  async function navigateTo(number) {
    if (!activeCatalog || !activeCatalog.editions.includes(number)) return;
    const response = await fetch(`/edition/${number}/edition.json`, { cache: 'no-store' });
    if (!response.ok) return;
    renderEdition(await response.json(), activeCatalog);
    window.history.pushState({}, '', `/${number}`);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  window.addEventListener('popstate', () => {
    const number = requestedEdition(activeCatalog);
    navigateTo(number);
  });

  load().catch(() => { document.body.classList.add('ready'); showError('לא הצלחנו לטעון את המהדורה. המהדורה האחרונה שנשמרה עדיין מוצגת.'); });
})();
