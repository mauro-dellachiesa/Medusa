# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import io
import logging
import os
import zipfile
from builtins import str

from babelfish import Language

from guessit import guessit

from requests import Session

from six import itervalues

from subliminal import Provider
from subliminal.cache import SHOW_EXPIRATION_TIME, region
from subliminal.exceptions import ProviderError
from subliminal.matches import guess_matches
from subliminal.subtitle import Subtitle, fix_line_ending
from subliminal.utils import sanitize
from subliminal.video import Episode, Movie

logger = logging.getLogger(__name__)


class WizdomSubtitle(Subtitle):
    """Wizdom Subtitle."""
    provider_name = 'wizdom'

    def __init__(self, language, hearing_impaired, page_link, series,
                 season, episode, title, imdb_id, subtitle_id, release):

        super(WizdomSubtitle, self).__init__(language, hearing_impaired, page_link)
        self.series = series
        self.season = season
        self.episode = episode
        self.title = title
        self.imdb_id = imdb_id
        self.subtitle_id = subtitle_id
        self.downloaded = 0
        self.release = release

    @property
    def id(self):
        return str(self.subtitle_id)

    def get_matches(self, video):
        matches = set()

        # episode
        if isinstance(video, Episode):
            # series
            if video.series and sanitize(self.series) == sanitize(video.series):
                matches.add('series')
            # season
            if video.season and self.season == video.season:
                matches.add('season')
            # episode
            if video.episode and self.episode == video.episode:
                matches.add('episode')
            # imdb_id
            if video.series_imdb_id and self.imdb_id == video.series_imdb_id:
                matches.add('series_imdb_id')
            # guess
            matches |= guess_matches(video, guessit(self.release, {'type': 'episode'}))

        # movie
        elif isinstance(video, Movie):
            # guess
            matches |= guess_matches(video, guessit(self.release, {'type': 'movie'}))

        # title
        if video.title and sanitize(self.title) == sanitize(video.title):
            matches.add('title')

        return matches


class WizdomProvider(Provider):
    """Wizdom Provider."""
    languages = {Language.fromalpha2(l) for l in ['he']}
    server_url = 'wizdom.xyz'

    _tmdb_api_key = 'f7f51775877e0bb6703520952b3c7840'

    def __init__(self):
        self.session = None

    def initialize(self):
        self.session = Session()
        self.session.headers['User-Agent'] = self.user_agent

    def terminate(self):
        self.session.close()

    @region.cache_on_arguments(expiration_time=SHOW_EXPIRATION_TIME)
    def _search_imdb_id(self, title, year, is_movie):
        """Search the IMDB ID for the given `title` and `year`.

        :param str title: title to search for.
        :param int year: year to search for (or 0 if not relevant).
        :param bool is_movie: If True, IMDB ID will be searched for in TMDB instead of Wizdom.
        :return: the IMDB ID for the given title and year (or None if not found).
        :rtype: str

        """
        # make the search
        logger.info('Searching IMDB ID for %r%r', title, '' if not year else ' ({})'.format(year))
        category = 'movie' if is_movie else 'tv'
        title = title.replace("'", '')
        # get TMDB ID first
        r = self.session.get('http://api.tmdb.org/3/search/{}?api_key={}&query={}{}&language=en'.format(
            category, self._tmdb_api_key, title, '' if not year else '&year={}'.format(year)))
        r.raise_for_status()
        tmdb_results = r.json().get('results')
        if tmdb_results:
            tmdb_id = tmdb_results[0].get('id')
            if tmdb_id:
                # get actual IMDB ID from TMDB
                r = self.session.get('http://api.tmdb.org/3/{}/{}{}?api_key={}&language=en'.format(
                    category, tmdb_id, '' if is_movie else '/external_ids', self._tmdb_api_key))
                r.raise_for_status()
                return str(r.json().get('imdb_id', '')) or None
        return None

    def _get_subtitles_for_episode(self, imdb_id, season, episode):
        """Search for subtitle by imdb_id, season and episode."""
        logger.debug('Using IMDB ID %r', imdb_id)

        url = f'https://{self.server_url}/api/search?action=by_id&imdb={imdb_id}&season={season}&episode={episode}'
        results = []
        try:
            # search
            response = self.session.get(url)
            response.raise_for_status()
            results = response.json()
        except (Exception, ValueError):
            logger.warning('Error trying to search for subtitle for imdb_id: %s, season: %s, episode: %s',
                           imdb_id, season, episode)
        return results

    def query(self, title, season=None, episode=None, year=None, filename=None, imdb_id=None):
        # search for the IMDB ID if needed.
        is_movie = not (season and episode)
        imdb_id = imdb_id or self._search_imdb_id(title, year, is_movie)
        if not imdb_id:
            return {}

        page_link = 'https://{}/#/{}/{}'.format(self.server_url, 'movies' if is_movie else 'series', imdb_id)

        # get the list of subtitles
        logger.debug('Getting the list of subtitles')

        # Get subtitles
        results = self._get_subtitles_for_episode(imdb_id, season, episode)

        # loop over results
        subtitles = {}
        for result in results:
            language = Language.fromalpha2('he')
            hearing_impaired = False
            subtitle_id = result['id']
            release = result['versioname']

            # otherwise create it
            subtitle = WizdomSubtitle(language, hearing_impaired, page_link, title, season, episode, title, imdb_id,
                                      subtitle_id, release)
            logger.debug('Found subtitle %r', subtitle)
            subtitles[subtitle_id] = subtitle

        return list(itervalues(subtitles))

    def list_subtitles(self, video, languages):
        season = episode = None
        title = video.title
        year = video.year
        filename = video.name
        imdb_id = video.imdb_id

        if isinstance(video, Episode):
            title = video.series
            season = video.season
            episode = video.episode
            imdb_id = video.series_imdb_id

        return [s for s in self.query(title, season, episode, year, filename, imdb_id) if s.language in languages]

    def download_subtitle(self, subtitle):
        # download
        url = f'https://{self.server_url}/api/files/sub/{subtitle.subtitle_id}'
        r = self.session.get(url, headers={'Referer': subtitle.page_link}, timeout=10)
        r.raise_for_status()

        # open the zip
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            # remove some filenames from the namelist
            namelist = [n for n in zf.namelist() if os.path.splitext(n)[1] in ['.srt', '.sub']]
            if len(namelist) > 1:
                raise ProviderError('More than one file to unzip')

            subtitle.content = fix_line_ending(zf.read(namelist[0]))
