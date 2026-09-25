(() => {
  const catalogUrl = '/edition/catalog.json';
  const editionPattern = /^\/(-?\d{3})\/?$/;
  let activeCatalog;
  let previousEdition = null;
  let nextEdition = null;

  const escapeHtml = (value) => String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');

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
    return value.replace('T', ' ');
  }

  function sourcePublisher(source) {
    if (source.name || source.publisher) return source.name || source.publisher;
    try { return new URL(source.url).hostname.replace(/^www\./, ''); } catch { return 'מקור'; }
  }

  function sourceRole(source) {
    const role = source.primarySecondary === 'primary' ? 'מקור ראשי' : source.primarySecondary === 'secondary' ? 'מקור משני' : 'תפקיד המקור לא צוין';
    const provenance = source.originalOrSyndicated === 'republished' || source.originalOrSyndicated === 'syndicated' ? 'דיווח מסונדיקט/מפורסם מחדש' : source.originalOrSyndicated === 'original' ? 'דיווח מקורי' : 'מקוריות לא ידועה';
    return `${role} · ${provenance}`;
  }

  function sourceMetadata(source) {
    const rows = [];
    rows.push(`<dt>מפרסם</dt><dd>${escapeHtml(sourcePublisher(source))}</dd>`);
    rows.push(`<dt>תפקיד</dt><dd>${escapeHtml(sourceRole(source))}</dd>`);
    if (source.sourceType) rows.push(`<dt>סוג</dt><dd>${escapeHtml(source.sourceType)}</dd>`);
    if (source.trustStatus) rows.push(`<dt>סטטוס אמון</dt><dd>${escapeHtml(source.trustStatus)}</dd>`);
    if (source.published_date_displayed) rows.push(`<dt>תאריך המו״ל</dt><dd>${escapeHtml(source.published_date_displayed)}</dd>`);
    if (source.published_at_source) rows.push(`<dt>מועד המקור</dt><dd dir="ltr">${escapeHtml(source.published_at_source)}</dd>`);
    rows.push(`<dt>אזור זמן של המקור</dt><dd dir="ltr">${escapeHtml(source.published_timezone || 'unknown')}</dd>`);
    if (source.published_at_jerusalem) rows.push(`<dt>מועד ירושלים</dt><dd dir="ltr">${escapeHtml(source.published_at_jerusalem)}</dd>`);
    if (source.published_at_jerusalem || source.published_date_jerusalem) rows.push(`<dt>אזור הנרמול</dt><dd dir="ltr">Asia/Jerusalem</dd>`);
    if (source.published_date_jerusalem || source.normalization_status === 'date_only') rows.push(`<dt>תאריך ירושלים מנורמל</dt><dd dir="ltr">${escapeHtml(source.published_date_jerusalem || 'unknown')}</dd>`);
    if (source.normalization_status) rows.push(`<dt>סטטוס נרמול</dt><dd>${escapeHtml(source.normalization_status)}</dd>`);
    if (source.date_modified_displayed) rows.push(`<dt>עדכון כפי שהוצג</dt><dd>${escapeHtml(source.date_modified_displayed)}</dd>`);
    if (source.date_modified_source) rows.push(`<dt>dateModified</dt><dd dir="ltr">${escapeHtml(source.date_modified_source)}</dd>`);
    rows.push(`<dt>אזור זמן של העדכון</dt><dd dir="ltr">${escapeHtml(source.date_modified_timezone || 'unknown')}</dd>`);
    if (source.date_modified_at_jerusalem) rows.push(`<dt>עדכון בירושלים</dt><dd dir="ltr">${escapeHtml(source.date_modified_at_jerusalem)}</dd>`);
    if (source.date_modified_date_jerusalem || source.date_modified_normalization_status === 'date_only') rows.push(`<dt>תאריך עדכון ירושלים מנורמל</dt><dd dir="ltr">${escapeHtml(source.date_modified_date_jerusalem || 'unknown')}</dd>`);
    if (source.date_modified_normalization_status) rows.push(`<dt>סטטוס נרמול עדכון</dt><dd>${escapeHtml(source.date_modified_normalization_status)}</dd>`);
    if (source.contribution) rows.push(`<dt>תרומת המקור</dt><dd>${escapeHtml(source.contribution)}</dd>`);
    if (source.syndicationRelation) rows.push(`<dt>סינדיקציה</dt><dd>${escapeHtml(source.syndicationRelation)}</dd>`);
    if (source.independentEvidence !== undefined) rows.push(`<dt>ראיה בלתי תלויה</dt><dd>${source.independentEvidence ? 'כן' : 'לא'}</dd>`);
    return `<dl class="source-metadata">${rows.join('')}</dl>`;
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
    const label = sourcePublisher(source);
    const link = `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer" aria-label="${escapeHtml(label)} — מקור ${index + 1} לסיפור ${escapeHtml(story.headline || '')} (נפתח בלשונית חדשה)">${escapeHtml(label)} ↗</a>`;
    return `<li class="source-item"><div class="source-link"><span class="source-number">${index + 1}.</span>${link}<span class="source-role">${escapeHtml(sourceRole(source))}</span></div>${sourceMetadata(source)}</li>`;
  }

  function renderSourceList(story) {
    const sources = Array.isArray(story.sources) ? story.sources : [];
    return `<div class="source-links"><p class="eyebrow">מקורות וראיות</p><ol>${sources.map((source, index) => renderSource(source, index, story)).join('')}</ol></div>`;
  }

  function renderLegacyStory(story, index) {
    const number = String(index + 1).padStart(2, '0');
    const sources = story.sources.map((source, sourceIndex) =>
      `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer" aria-label="מקור ${sourceIndex + 1} לכתבה בנושא ${escapeHtml(story.section)} (נפתח בלשונית חדשה)">${story.sources.length === 1 ? 'לכתבה המלאה' : `מקור ${sourceIndex + 1}`} ↗</a>`
    ).join('');
    return `<section class="story">
      <aside class="story-context"><p class="eyebrow">הקשר</p><p>${escapeHtml(story.whyItMatters)}</p><div class="source-links"><p class="eyebrow">מקורות</p>${sources}</div></aside>
      <div class="story-main"><p class="eyebrow story-label"><span>${escapeHtml(story.section)}</span></p><h2>${escapeHtml(story.headline)}</h2><p>${escapeHtml(story.summary)}</p></div>
      <div class="story-index"><b>${number}</b><span>${escapeHtml(story.readingTime)}</span></div>
    </section>`;
  }

  function renderWeeklyStory(story, index, sectionHeading = '') {
    const number = String(index + 1).padStart(2, '0');
    const section = story.section || sectionHeading;
    return `<section class="story">
      <aside class="story-context"><p class="eyebrow">הקשר</p><p>${escapeHtml(story.whyItMatters)}</p>${renderSourceList(story)}</aside>
      <div class="story-main"><p class="eyebrow story-label"><span>${escapeHtml(section)}</span></p><h2>${escapeHtml(story.headline)}</h2><p>${escapeHtml(story.summary)}</p></div>
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
      const description = section.description ? `<p>${escapeHtml(section.description)}</p>` : '';
      return `<section class="story-section" data-section-id="${escapeHtml(section.id || '')}"><header class="story-section-header"><h3>${escapeHtml(section.heading || '')}</h3>${description}</header>${rendered}</section>`;
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
    document.querySelector('meta[name="description"]').content = `${edition.headline} — ${edition.introduction}`;
    document.querySelector('meta[property="og:description"]').content = edition.editionType === 'weekly'
      ? `${edition.headline} — תדריך שבועי של חמש דקות על AI בעברית.`
      : `${edition.headline} — חמש דקות של חדשות AI בעברית.`;
    document.querySelector('.metadata .edition-label').innerHTML = `${label} <bdi>${escapeHtml(edition.number)}</bdi>`;
    const time = document.querySelector('.metadata time');
    time.dateTime = edition.publicationDate;
    time.textContent = formattedDate;

    const inside = document.querySelector('[data-edition-root]');
    inside.setAttribute('aria-label', `תוכן מהדורה ${edition.number}`);
    const header = inside.querySelector('.inside-header');
    const coverage = edition.coverageStart && edition.coverageEnd
      ? `<p class="coverage">חלון הסיקור: ${formatDate(edition.coverageStart)}–${formatDate(edition.coverageEnd)} · פורסם ${formattedDate}${modifiedDate ? ` · עודכן ${escapeHtml(modifiedDate)}` : ''}</p>`
      : '';
    const archiveNote = edition.editionType === 'weekly'
      ? '<p class="archive-note">המהדורות היומיות הקודמות נשמרות בארכיון ללא שינוי.</p>'
      : '';
    const weeklyMeta = edition.editionType === 'weekly'
      ? `<p>${label} ${edition.number} · פורסם ${formattedDate}${modifiedDate ? ` · עודכן ${escapeHtml(modifiedDate)}` : ''}</p>`
      : '';
    header.innerHTML = `${weeklyMeta}${coverage}<h2>${escapeHtml(edition.headline)}</h2><p>${escapeHtml(edition.introduction)}</p>${archiveNote}${edition.editorialNote ? `<p class="draft-note">${escapeHtml(edition.editorialNote)}</p>` : ''}<p class="ai-disclosure">${escapeHtml(edition.aiDisclosure)}</p>`;
    const storyCountLabels = { 4: 'ארבעת', 5: 'חמשת', 6: 'ששת' };
    const storyCountLabel = storyCountLabels[stories.length] || 'מספר';
    inside.querySelector('.quick-read').innerHTML = `<h3>במבט אחד · ${storyCountLabel} הנושאים</h3><ul>${stories.map((story) => `<li><strong>${escapeHtml(story.section || '')}:</strong> ${escapeHtml(story.quickRead)}</li>`).join('')}</ul>`;
    inside.querySelector('.stories').innerHTML = renderStories(edition, stories);
    inside.querySelector('.takeaway').innerHTML = `<p class="eyebrow">${escapeHtml(edition.totalReadingTime)} · השורה התחתונה</p><h2>${escapeHtml(edition.takeaway)}</h2>`;

    const index = catalog.editions.indexOf(edition.number);
    const previous = catalog.editions[index - 1];
    const next = catalog.editions[index + 1];
    const previousLinks = document.querySelectorAll('[data-previous]');
    const nextLinks = document.querySelectorAll('[data-next]');
    previousLinks.forEach((link) => {
      link.hidden = !previous;
      if (previous) link.href = `/${previous}`;
    });
    nextLinks.forEach((link) => {
      link.hidden = !next;
      if (next) link.href = `/${next}`;
    });
    [...previousLinks, ...nextLinks].forEach((link) => {
      link.onclick = (event) => { event.preventDefault(); navigateTo(link.hasAttribute('data-previous') ? previous : next); };
    });
    document.querySelector('.edition-status').textContent = `${label} ${edition.number}`;
    installSwipe(previous, next);
    installKeyboard(previous, next);
  }

  function installSwipe(previous, next) {
    previousEdition = previous;
    nextEdition = next;
    if (installSwipe.ready) return;
    installSwipe.ready = true;
    let startX = 0;
    let startY = 0;
    document.addEventListener('touchstart', (event) => {
      startX = event.changedTouches[0].clientX;
      startY = event.changedTouches[0].clientY;
    }, { passive: true });
    document.addEventListener('touchend', (event) => {
      const deltaX = event.changedTouches[0].clientX - startX;
      const deltaY = event.changedTouches[0].clientY - startY;
      if (Math.abs(deltaX) < 70 || Math.abs(deltaX) < Math.abs(deltaY) * 1.25) return;
      if (deltaX > 0 && previousEdition) navigateTo(previousEdition);
      if (deltaX < 0 && nextEdition) navigateTo(nextEdition);
    }, { passive: true });
  }

  function installKeyboard(previous, next) {
    previousEdition = previous;
    nextEdition = next;
    if (installKeyboard.ready) return;
    installKeyboard.ready = true;
    document.addEventListener('keydown', (event) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (event.key === 'ArrowRight' && previousEdition) navigateTo(previousEdition);
      if (event.key === 'ArrowLeft' && nextEdition) navigateTo(nextEdition);
    });
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
      showError('המהדורה המבוקשת אינה קיימת.');
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
