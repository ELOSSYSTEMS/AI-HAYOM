(() => {
  const catalogUrl = '/editions/catalog.json';
  const editionPattern = /^\/(\d{3})\/?$/;

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
    const [year, month, day] = date.split('-');
    return `${day}.${month}.${year}`;
  }

  function renderStory(story, index) {
    const number = String(index + 1).padStart(2, '0');
    const sources = story.sources.map((source, sourceIndex) =>
      `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer" aria-label="מקור ${sourceIndex + 1} לכתבה בנושא ${escapeHtml(story.section)} (נפתח בלשונית חדשה)">${story.sources.length === 1 ? 'לכתבה המלאה' : `מקור ${sourceIndex + 1}`} ↗</a>`
    ).join('');
    return `<section class="story">
      <div class="story-index"><b>${number}</b><span>${escapeHtml(story.readingTime)}</span></div>
      <div><p class="eyebrow story-label"><span>${escapeHtml(story.section)}</span>${sources}</p><h2>${escapeHtml(story.headline)}</h2><p>${escapeHtml(story.summary)}</p></div>
      <div class="why"><p class="eyebrow">למה זה חשוב</p><p>${escapeHtml(story.whyItMatters)}</p></div>
    </section>`;
  }

  function renderEdition(edition, catalog) {
    const formattedDate = formatDate(edition.publicationDate);
    document.title = `AI היום | מהדורה ${edition.number}`;
    document.querySelector('.front').setAttribute('aria-label', `שער מהדורה ${edition.number}`);
    document.querySelector('.metadata bdi').textContent = edition.number;
    const time = document.querySelector('.metadata time');
    time.dateTime = edition.publicationDate;
    time.textContent = formattedDate;
    document.querySelector('.headline h1').textContent = edition.headline;
    const source = document.querySelector('.cartoon source');
    const image = document.querySelector('.cartoon img');
    source.srcset = `/${edition.cartoon.mobile}`;
    image.src = `/${edition.cartoon.desktop}`;
    image.alt = edition.cartoon.alt;
    document.querySelector('.description').textContent = edition.coverDescription;
    document.querySelector('.topics').innerHTML = `${edition.keywords.map((keyword) => `<span>${escapeHtml(keyword)}</span>`).join('')}<i class="dot" aria-hidden="true"></i>`;

    const inside = document.querySelector('[data-edition-root]');
    inside.setAttribute('aria-label', `תוכן מהדורה ${edition.number}`);
    const header = inside.querySelector('.inside-header');
    header.innerHTML = `<p>מהדורה ${edition.number} · ${formattedDate}</p><h2>${escapeHtml(edition.headline)}</h2><p>${escapeHtml(edition.introduction)}</p>${edition.editorialNote ? `<p class="draft-note">${escapeHtml(edition.editorialNote)}</p>` : ''}<p class="ai-disclosure">${escapeHtml(edition.aiDisclosure)}</p>`;
    inside.querySelector('.quick-read').innerHTML = `<h3>במבט אחד · ${edition.stories.length === 4 ? 'ארבעת' : 'חמשת'} הנושאים</h3><ul>${edition.stories.map((story) => `<li><strong>${escapeHtml(story.section)}:</strong> ${escapeHtml(story.quickRead)}</li>`).join('')}</ul>`;
    inside.querySelector('.stories').innerHTML = edition.stories.map(renderStory).join('');
    inside.querySelector('.takeaway').innerHTML = `<p class="eyebrow">${escapeHtml(edition.totalReadingTime)} · השורה התחתונה</p><h2>${escapeHtml(edition.takeaway)}</h2>`;

    const index = catalog.editions.indexOf(edition.number);
    const previous = catalog.editions[index - 1];
    const next = catalog.editions[index + 1];
    const previousLink = document.querySelector('[data-previous]');
    const nextLink = document.querySelector('[data-next]');
    previousLink.hidden = !previous;
    nextLink.hidden = !next;
    if (previous) previousLink.href = `/${previous}`;
    if (next) nextLink.href = `/${next}`;
    document.querySelector('.edition-status').textContent = `מהדורה ${edition.number}`;
    installSwipe(previous, next);
  }

  function installSwipe(previous, next) {
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
      if (deltaX > 0 && previous) window.location.assign(`/${previous}`);
      if (deltaX < 0 && next) window.location.assign(`/${next}`);
    }, { passive: true });
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
    const number = requestedEdition(catalog);
    if (!catalog.editions.includes(number)) {
      showError('המהדורה המבוקשת אינה קיימת.');
      return;
    }
    const editionResponse = await fetch(`/editions/${number}.json`);
    if (!editionResponse.ok) throw new Error('edition');
    renderEdition(await editionResponse.json(), catalog);
  }

  load().catch(() => showError('לא הצלחנו לטעון את המהדורה. המהדורה האחרונה שנשמרה עדיין מוצגת.'));
})();
