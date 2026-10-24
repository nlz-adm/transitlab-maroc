const DEFAULT_MIN_ZOOM = 8;

export function validateDouarIndex(index, expectedVersion) {
  if (!index || typeof index !== 'object') throw new Error('Index des douars absent ou invalide');
  if (!expectedVersion || index.version !== expectedVersion) {
    throw new Error('Version de l’index des douars inattendue');
  }
  const regions = index.regions;
  if (!Array.isArray(regions) || regions.length !== 12) {
    throw new Error('L’index des douars doit contenir les 12 régions');
  }
  const total = Number(index.features);
  const located = Number(index.locatedPoints);
  const unlocated = Number(index.unlocatedFeatures);
  if (!Number.isInteger(total) || total < 30000
      || !Number.isInteger(located) || located < 10000
      || !Number.isInteger(unlocated) || total !== located + unlocated) {
    throw new Error('Compteurs nationaux des douars incohérents');
  }
  const codes = new Set();
  let regionalTotal = 0;
  let regionalPoints = 0;
  for (const region of regions) {
    const code = String(region?.code || '');
    if (!/^\d{2}$/.test(code) || codes.has(code)) {
      throw new Error('Code de région absent ou dupliqué dans l’index des douars');
    }
    codes.add(code);
    const count = Number(region.count);
    const pointCount = Number(region.pointCount);
    const unlocatedCount = Number(region.unlocatedCount);
    if (!region.url || !String(region.url).startsWith('assets/douars/')
        || !String(region.url).endsWith('.geojson.gz')
        || !Number.isInteger(count) || count < 1
        || !Number.isInteger(pointCount) || pointCount < 0
        || !Number.isInteger(unlocatedCount) || count !== pointCount + unlocatedCount) {
      throw new Error('Statistiques régionales invalides pour la région ' + code);
    }
    regionalTotal += count;
    regionalPoints += pointCount;
  }
  if (regionalTotal !== total || regionalPoints !== located) {
    throw new Error('Les compteurs régionaux ne correspondent pas à l’index national');
  }
  return index;
}

export function isDouarPointVisible(feature, bounds) {
  const geometry = feature?.geometry;
  if (geometry?.type !== 'Point' || !Array.isArray(geometry.coordinates) || geometry.coordinates.length !== 2) return false;
  const longitude = Number(geometry.coordinates[0]);
  const latitude = Number(geometry.coordinates[1]);
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude) || !bounds?.contains) return false;
  return Boolean(bounds.contains([latitude, longitude]));
}

export function attachDouarLayer(options) {
  const {
    map, L, doc = document, expectedVersion, writeCachedJson, readCachedJson, escapeHtml,
  } = options || {};
  const toggle = doc?.getElementById('douarsToggle');
  const status = doc?.getElementById('douarLayerStatus');
  if (!map || !L || !toggle || !status || !expectedVersion
      || typeof readCachedJson !== 'function' || typeof writeCachedJson !== 'function'
      || typeof escapeHtml !== 'function') return null;

  const regionData = new Map();
  const regionPromises = new Map();
  const activeLayers = new Map();
  let index = null;
  let indexPromise = null;
  let refreshId = 0;
  let lastError = '';

  function setStatus(message) {
    status.textContent = message;
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString('fr-FR');
  }

  function bboxIntersectsBounds(bounds, bbox) {
    return Boolean(bounds && bbox && bbox.length === 4
      && bbox[0] <= bounds.getEast() && bbox[2] >= bounds.getWest()
      && bbox[1] <= bounds.getNorth() && bbox[3] >= bounds.getSouth());
  }

  function removeLayers(keep = new Set()) {
    for (const [code, layer] of activeLayers) {
      if (keep.has(code)) continue;
      if (map.hasLayer(layer)) map.removeLayer(layer);
      activeLayers.delete(code);
    }
  }

  async function loadIndex() {
    if (index) return index;
    if (!indexPromise) {
      indexPromise = (async () => {
        const url = new URL('assets/douars-index.json?v=' + encodeURIComponent(expectedVersion), doc.baseURI);
        const response = await fetch(url, { cache: 'no-store' });
        if (!response.ok) throw new Error('Chargement de l’index des douars impossible (' + response.status + ')');
        const value = validateDouarIndex(await response.json(), expectedVersion);
        index = value;
        await writeCachedJson(String(url), value);
        return value;
      })().catch(error => {
        indexPromise = null;
        throw error;
      });
    }
    return indexPromise;
  }

  async function loadRegion(region, sourceIndex) {
    if (regionData.has(region.code)) return regionData.get(region.code);
    if (regionPromises.has(region.code)) return regionPromises.get(region.code);
    const url = new URL(region.url, doc.baseURI);
    url.searchParams.set('v', sourceIndex.version);
    url.searchParams.set('generated', String(sourceIndex.generatedAt || ''));
    const key = String(url);
    const promise = (async () => {
      const cached = await readCachedJson(key);
      if (cached !== undefined) {
        if (cached?.type !== 'FeatureCollection' || !Array.isArray(cached.features)
            || cached.features.length !== Number(region.count)) {
          throw new Error('Le cache de la région ' + region.code + ' est incohérent');
        }
        regionData.set(region.code, cached.features);
        return cached.features;
      }
      const response = await fetch(url, { cache: 'no-store' });
      if (!response.ok) throw new Error('Chargement des douars de la région ' + region.code + ' impossible (' + response.status + ')');
      if (typeof DecompressionStream === 'undefined') {
        throw new Error('Ce navigateur ne sait pas décompresser le fichier des douars');
      }
      const compressed = await response.arrayBuffer();
      const stream = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip'));
      const value = JSON.parse(await new Response(stream).text());
      if (value?.type !== 'FeatureCollection' || !Array.isArray(value.features)
          || value.features.length !== Number(region.count)) {
        throw new Error('Le fichier des douars de la région ' + region.code + ' est incomplet');
      }
      let pointCount = 0;
      for (const feature of value.features) {
        const geometry = feature?.geometry;
        const properties = feature?.properties || {};
        if (geometry == null) {
          if (properties.geometryStatus !== 'unlocated') {
            throw new Error('Statut incohérent pour un douar sans géométrie');
          }
          continue;
        }
        if (geometry.type !== 'Point' || !Array.isArray(geometry.coordinates)
            || geometry.coordinates.length !== 2
            || !geometry.coordinates.every(coordinate => Number.isFinite(Number(coordinate)))
            || properties.geometryStatus !== 'mapped_point') {
          throw new Error('Géométrie de douar invalide : seuls les points sourcés sont acceptés');
        }
        pointCount += 1;
      }
      if (pointCount !== Number(region.pointCount)) {
        throw new Error('Le nombre de points de la région ' + region.code + ' ne correspond pas à l’index');
      }
      await writeCachedJson(key, value);
      regionData.set(region.code, value.features);
      return value.features;
    })().finally(() => regionPromises.delete(region.code));
    regionPromises.set(region.code, promise);
    return promise;
  }

  function regionSignature(features) {
    return features.map(feature => String(feature.id || feature.properties?.code || '')).join('|');
  }

  function createRegionLayer(code, features, zoom) {
    const points = features.filter(feature => isDouarPointVisible(feature, map.getBounds()));
    if (!points.length) {
      removeLayers(new Set([...activeLayers.keys()].filter(key => key !== code)));
      const prior = activeLayers.get(code);
      if (prior && map.hasLayer(prior)) map.removeLayer(prior);
      activeLayers.delete(code);
      return;
    }
    const signature = regionSignature(points);
    const prior = activeLayers.get(code);
    if (prior?.__douarSignature === signature) return;
    if (prior && map.hasLayer(prior)) map.removeLayer(prior);
    const layer = L.geoJSON({ type: 'FeatureCollection', features: points }, {
      pane: 'douarsPane',
      renderer: L.canvas({ padding: 0.15 }),
      pointToLayer(feature, latlng) {
        return L.circleMarker(latlng, {
          pane: 'douarsPane',
          radius: zoom >= 11 ? 4.2 : 3.3,
          color: '#ffffff',
          weight: 1.1,
          opacity: 0.95,
          fillColor: '#7c3aed',
          fillOpacity: 0.92,
        });
      },
      onEachFeature(feature, itemLayer) {
        const properties = feature.properties || {};
        const name = String(properties.name_fr || properties.name || properties.name_ar || 'Douar sans nom');
        const arabic = String(properties.name_ar || '');
        const commune = String(properties.communeCode || 'Non précisée');
        const source = properties.locationSource === 'osm' ? 'OpenStreetMap' : 'GeoNames';
        const method = properties.locationMatchMethod === 'near'
          ? 'Lieu proche associé dans la même fraction'
          : 'Correspondance de nom avec le référentiel du recensement';
        const population = formatNumber(properties.population);
        const households = formatNumber(properties.households);
        const popup = '<strong>' + escapeHtml(name) + '</strong>'
          + (arabic && arabic !== name ? '<br><span dir="rtl">' + escapeHtml(arabic) + '</span>' : '')
          + '<br><small>Douar rural · HCP RGPH 2024</small>'
          + '<br><strong>Population :</strong> ' + population
          + '<br><strong>Ménages :</strong> ' + households
          + '<br><strong>Code commune HCP :</strong> ' + escapeHtml(commune)
          + '<br><small>Localisation : ' + escapeHtml(source) + ' · ' + escapeHtml(method) + '.</small>'
          + '<br><small>Ce point indique un lieu, pas le contour officiel du douar.</small>'
          + '<br><a href="https://www.hcp.ma/Population-et-menages-par-douars-selon-les-resultats-du-recensement-general-de-la-population-et-de-l-habitat-de-2024_a4208.html" target="_blank" rel="noopener">Source HCP RGPH 2024 ↗</a>';
        itemLayer.bindPopup(popup);
        if (zoom >= 10) itemLayer.bindTooltip(escapeHtml(name), { direction: 'top', opacity: 0.92 });
      },
    });
    layer.__douarSignature = signature;
    const existing = activeLayers.get(code);
    if (existing && map.hasLayer(existing)) map.removeLayer(existing);
    activeLayers.set(code, layer);
    layer.addTo(map);
  }

  async function refresh() {
    const id = ++refreshId;
    if (!toggle.checked) {
      removeLayers();
      setStatus('Couche désactivée. Les douars restent séparés des quartiers urbains.');
      return;
    }
    const zoom = map.getZoom();
    if (zoom < DEFAULT_MIN_ZOOM) {
      removeLayers();
      setStatus('Zoome au niveau 8 ou plus pour afficher les douars ruraux localisés.');
      return;
    }
    setStatus('Chargement des douars du RGPH 2024…');
    try {
      const sourceIndex = await loadIndex();
      if (id !== refreshId || !toggle.checked) return;
      const bounds = map.getBounds().pad(0.07);
      const visibleRegions = sourceIndex.regions.filter(region => bboxIntersectsBounds(bounds, region.bbox));
      const keep = new Set(visibleRegions.map(region => region.code));
      removeLayers(keep);
      if (!visibleRegions.length) {
        setStatus('Aucun point de douar sourcé dans cette vue. Déplace la carte ou zoome ailleurs.');
        return;
      }
      const groups = await Promise.all(visibleRegions.map(async region => ({
        region,
        features: await loadRegion(region, sourceIndex),
      })));
      if (id !== refreshId || !toggle.checked) return;
      for (const group of groups) createRegionLayer(group.region.code, group.features, zoom);
      setStatus(
        'RGPH 2024 : ' + formatNumber(sourceIndex.features) + ' douars ruraux ; '
        + formatNumber(sourceIndex.locatedPoints) + ' points sourcés, '
        + formatNumber(sourceIndex.unlocatedFeatures) + ' sans localisation fiable. '
        + 'Affichage à partir du zoom 8 ; un point n’est pas une frontière.'
      );
      lastError = '';
    } catch (error) {
      if (id !== refreshId) return;
      const message = error?.message || 'chargement impossible';
      if (message !== lastError) {
        setStatus('Couche des douars indisponible : ' + message);
        lastError = message;
      }
    }
  }

  toggle.addEventListener('change', refresh);
  map.on('zoomend moveend', () => {
    if (toggle.checked) refresh();
  });
  return { refresh, dispose() { refreshId += 1; removeLayers(); } };
}
