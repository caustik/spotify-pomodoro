#
# spotify.py
#
# author: caustik
# desc: generate random pomodoro length playlists from user's tracks
# usage: set environment variables CLIENT_ID and CLIENT_SECRET, run spotify.py and connect via HTTP
#

import multiprocessing, subprocess, requests, datetime, itertools, argparse, asyncio, random, time, json, sys, os, io

from dateutil.parser import parse as date_parse
from urllib.parse import urlparse, parse_qs
from tornado import websocket, web, ioloop, httpserver, httpclient, gen
from itertools import product

def fetch_http(url, headers):
    return requests.get(url, headers = headers)

class CommandLine:
    def __init__(self):
        # prepare command line argument parser
        commands = [ "server" ]
        arg_parser = argparse.ArgumentParser(description="generate random pomodoro length playlists from user's tracks", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        arg_parser.add_argument('-c', dest='command', choices=commands, required=False, default="server", help='command')
        # parse command line arguments
        self.args = arg_parser.parse_args()
        # execute command
        getattr(globals()['CommandLine'], self.args.command)(self)

    def server(self):
        # create application with our url handlers
        application = web.Application([
            (r'/api', ApiHandler),
            (r'/(.*)', NoCacheStaticFileHandler, { 'path': './static', 'default_filename': 'index.html' }),
        ])
        # startup http server
        http_server = httpserver.HTTPServer(application, xheaders=True)
        http_server.listen(80)
        # start the IO loop
        print("Starting server (press ctrl + break/pause to terminate")
        ioloop.IOLoop.current().start()

class NoCacheStaticFileHandler(web.StaticFileHandler):
    def set_extra_headers(self, path):
        self.set_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')

class ApiHandler(websocket.WebSocketHandler):
    # spotify API authentication
    client_id = os.environ["CLIENT_ID"]
    client_secret = os.environ["CLIENT_SECRET"]

    # HTTP configuration
    parallel_http = 16

    # human readable LUTs
    strategy_names = [ "+5", "+6", "+0", "explicit" ]
    key_names = [ "C", "C#", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B" ]
    mode_names = [ "Major", "minor" ]
    mode_suffix = [ "", "m" ]
    explicit = [ "E minor", "E minor", "C Major", "C minor", "E Major" ]

    http = httpclient.AsyncHTTPClient()

    def check_origin(self, origin):
        return True

    def open(self, *args):
        self.send_message("Connected")

    @gen.coroutine
    def on_message(self, message):
        parsed = json.loads(message)
        if parsed["type"] == "authenticate":
            self.authenticate(parsed)
        elif parsed["type"] == "load_tracks":
            self.load_tracks(parsed)
        elif parsed["type"] == "generate":
            self.generate(parsed)

    def send_message(self, text):
        self.write_message(json.dumps({
            "type": "message",
            "text": text
        }))

    @gen.coroutine
    def authenticate(self, message):
        self.tracks = [ ]
        self.load_config()

        # Get access token or use cached if available and not expired
        if "expires_at" not in self.config or datetime.datetime.today() >= date_parse(self.config["expires_at"]):
            client_code = message["client_code"]
            redirect_uri = message["redirect_uri"]

            self.send_message("Fetching access token...")

            response = yield self.http.fetch("https://accounts.spotify.com/api/token", 
                method = 'POST',
                data = {
                'grant_type': 'authorization_code',
                'code': client_code,
                'redirect_uri': redirect_uri,
                'client_id': self.client_id,
                'client_secret': self.client_secret
                }
            )
            auth = json.loads(response.body.decode())

            if "access_token" not in auth:
                self.write_message(json.dumps( {
                    "type": "authenticate",
                    "client_id": self.client_id
                }))
                return

            self.config["access_token"] = auth['access_token']
            self.config["expires_at"] = str(datetime.datetime.today() + datetime.timedelta(seconds = int(auth['expires_in'])))
            self.save_config()
        else:
            self.send_message("Using cached access token.")

        self.access_token = self.config["access_token"]

        # Get spotify user id
        self.send_message("Fetching user ID...")

        response = yield self.http.fetch("https://api.spotify.com/v1/me", 
            headers = { 
                'Authorization' : 'Bearer {:s}'.format(self.access_token) 
            }
        )
        self.user_id = json.loads(response.body.decode())['id']

        self.send_message("User ID: {}".format(self.user_id))
        
        # Load cached user data
        try:
            with open ('cache/{}.json'.format(self.user_id), 'r') as inpfile:
                self.user = json.load(inpfile)
            self.send_message("Loaded {} tracks".format(len(self.user['tracks'])))
        except:
            self.user = { 'tracks': [ ], 'ETag': None }

    @gen.coroutine
    def load_tracks(self, message):
        self.authenticate(message)

        etag = self.user['ETag']    # used for cache-control, but seems very unreliable
        offset = 0                  # current offset into tracks
        total = 50                  # total number of tracks, is revised after each page of results

        # Get the user's saved songs
        while offset < total:
            # calculate number of pages to fetch at this iteration
            parallel = int(min(((total - offset) + 49) / 50, self.parallel_http))
            # perform parallel HTTP fetches
            fetches = [ ]
            for i in range(parallel):
                url = "https://api.spotify.com/v1/users/{:s}/tracks?offset={}&limit=50".format(self.user_id, offset)
                fetches.append(self.http.fetch(url, 
                    headers = {
                        'Authorization' : 'Bearer {:s}'.format(self.access_token),
                        'If-None-Match' : self.user['ETag']
                    }
                ))
                offset = offset + 50
                self.send_message("Fetching {}...".format(url))
            # wait for responses
            tasks = yield fetches
            # validate HTTP status codes, restarting on failure
            valid = True;
            for response in tasks:
                if response.code != 200:
                    self.send_message("Error: Server returned HTTP {}, retrying...".format(response.status_code))
                    offset = offset - len(tasks)*50
                    valid = False
            if not valid:
                continue
            # parse tracks from the set of responses
            for response in tasks:
                if response.code == 304:
                    self.send_message("Using cached tracks")
                    self.tracks = self.user['tracks']
                    break
                elif etag == None:
                    etag = response.headers["ETag"]
                response = json.loads(response.body.decode())
                total = response['total']
                self.tracks = self.tracks + response["items"]

        try:
            os.mkdir('cache')
        except:
            pass

        etag = self.user['ETag']    # used for cache-control, but seems very unreliable
        offset = 0                  # current offset into tracks
        total = len(self.tracks)    # total number of tracks, is revised after each page of results
        responses = [ ]             # http responses

        # Get audio features for the user's songs
        while offset < total:
            # calculate number of pages to fetch at this iteration
            parallel = int(min(((total - offset) + 99) / 100, self.parallel_http))
            # perform parallel HTTP fetches
            fetches = [ ]
            for i in range(parallel):
                ids = map(lambda x: x["track"]["id"], self.tracks[offset:offset+100])
                ids = ','.join(ids)
                url = "https://api.spotify.com/v1/audio-features?ids={:s}".format(ids)
                fetches.append(self.http.fetch(url, 
                    headers = {
                        'Authorization' : 'Bearer {:s}'.format(self.access_token),
                        'If-None-Match' : self.user['ETag']
                    }
                ))
                offset = min(offset + 100, total)
                self.send_message("Fetching audio features ({}/{})".format(offset, len(self.tracks)))
            # wait for responses
            current_tasks = yield fetches
            # validate HTTP status codes, restarting on failure
            valid = True;
            for response in current_tasks:
                if response.code != 200:
                    self.send_message("Error: Server returned HTTP {}, retrying...".format(response.status_code))
                    offset = offset - len(current_tasks)*100
                    valid = False
            if not valid:
                continue

            responses = responses + current_tasks

        offset = 0
        # parse audio features from the set of responses
        for response in responses:
            if response.code == 304:
                self.send_message("Using cached audio information")
                break
            response = json.loads(response.body.decode())
            keys = list(map(lambda x: x["key"] if x and "key" in x else -1, response["audio_features"]))
            modes = list(map(lambda x: x["mode"] if x and "mode" in x else -1, response["audio_features"]))
            energy = list(map(lambda x: x["energy"] if x and "energy" in x else 1000, response["audio_features"]))
            for i in range(len(keys)):
                self.tracks[offset + i]["key"] = keys[i]
                self.tracks[offset + i]["mode"] = modes[i]
                self.tracks[offset + i]["energy"] = energy[i]
            offset = min(offset + len(keys), total)
                
        self.user['tracks'] = self.tracks
        self.user['ETag'] = etag

        self.send_message("Tracks were loaded.")

        # Save cached user data
        with open('cache/{}.json'.format(self.user_id), 'w') as outfile:
            json.dump(self.user, outfile)

    @gen.coroutine
    def generate(self, message):
        self.authenticate(message)

        random.seed()

        self.tracks = self.user['tracks']
        self.playlists = [ ]

        # Get the user's playlists
        url = "https://api.spotify.com/v1/users/{:s}/playlists?limit=50".format(self.user_id)
        while url != None:
            self.send_message("Fetching {}...".format(url))
            
            response = yield self.http.fetch(url, 
                headers = { 
                    'Authorization' : 'Bearer {:s}'.format(self.access_token)
                }
            );
            
            response = json.loads(response.body.decode())

            self.playlists = self.playlists + response["items"]
            url = response['next']

        # Generate the playlist
        self.send_message("Generating playlist...")
        self.key = message['key']
        self.mode = message['mode']
        self.strategy = message['strategy']
        self.toggle_major_minor = message['toggle_major_minor']
        self.toggle_state = 0
        self.playlist_duration = message['playlist_duration']
        self.playlist_duration_ms = self.playlist_duration * 60 * 1000
        self.min_duration_ms = message['min_duration'] * 60 * 1000
        self.max_duration_ms = message['max_duration'] * 60 * 1000
        self.min_energy = message['min_energy']
        self.max_energy = message['max_energy']

        self.send_message("Key: {} {}".format(self.key_names[self.key], self.mode_names[self.mode]))
        self.send_message("Strategy: {}".format(self.strategy_names[self.strategy]))

        self.tracklist = [ ]
        self.current_duration = 0

        while self.find_next():
            pass

        self.send_message("Target duration: {}, actual: {:.2f}".format(self.playlist_duration, self.current_duration / 1000 / 60))

        # Create the playlist, or find existing
        key_name = "{:s} {:s}".format(self.key_names[self.key] if self.key != -1 else "ALL", self.mode_names[self.mode] if self.mode != -1 else "ALL")
        playlist_name = message['playlist_name']

        playlist_url = self.find_playlist(playlist_name)
        if playlist_url == None:
            url = "https://api.spotify.com/v1/users/{:s}/playlists".format(self.user_id)
            self.send_message("Creating playlist {}".format(playlist_name))
            response = yield self.http.fetch(url, 
                method = 'POST',
                headers = { 
                    'Authorization' : 'Bearer {:s}'.format(self.access_token)
                }, 
                body = json.dumps({
                    'name': playlist_name,
                    'description': '{:d} minute pomodoro playlist in {:s}'.format(self.playlist_duration, key_name)
                })
            )
            playlist = json.loads(response.body.decode())
            playlist_url = playlist["tracks"]["href"]

        # Put the songs into the playlist
        self.send_message("Saving tracks to playlist {}".format(playlist_name))

        response = yield self.http.fetch(playlist_url, 
            method = 'PUT',
            headers = { 
                'Authorization' : 'Bearer {:s}'.format(self.access_token)
            }, 
            body = json.dumps({
                'uris': self.tracklist
            })
        )
        snapshot_id = json.loads(response.body.decode())

    def find_next(self):
        random.shuffle(self.tracks)
        if self.strategy == 3:
            split = self.explicit[len(self.tracklist) % len(self.explicit)].split(" ")
            self.key = self.key_names.index(split[0])
            self.mode = self.mode_names.index(split[1])
        for track in self.tracks:
            track_duration_ms = int(track["track"]["duration_ms"])
            if "used" in track:
                continue
            if track_duration_ms < self.min_duration_ms:
                continue
            if track_duration_ms > self.max_duration_ms:
                continue 
            if self.current_duration + track_duration_ms > self.playlist_duration_ms:
                continue 
            if self.key != -1 and track["key"] != self.key:
                continue
            if self.mode != -1 and track["mode"] != self.mode:
                continue
            if track["energy"] < self.min_energy or track["energy"] > self.max_energy:
                continue
            self.current_duration = self.current_duration + track_duration_ms
            self.tracklist.append(track["track"]["uri"])
            track["used"] = True
            self.send_message("{} {}".format(self.key_names[self.key], self.mode_names[self.mode]))
            if self.toggle_major_minor == 1:
                if self.toggle_state == 1:
                    self.mode = 1 - self.mode
                self.toggle_state = 1 - self.toggle_state
            if self.toggle_major_minor == 0 or self.toggle_state == 1:
                if self.strategy == 0:
                    self.key = (self.key + 5) % 12
                elif self.strategy == 1:
                    self.key = (self.key + 6) % 12
            return True

    def find_playlist(self, playlist_name):
        for playlist in self.playlists:
            if playlist["name"] == playlist_name:
                self.send_message("Found existing playlist: {:s}".format(playlist["href"]))
                return playlist["tracks"]["href"]
        return None

    def load_config(self):
        try:
            with open('config.json', 'r') as f:
                self.config = json.load(f)
        except:
            self.config = { }

    def save_config(self):
        with open('config.json', 'w') as f:
            json.dump(self.config, f)

    def grouper_it(self, n, iterable):
        it = iter(iterable)
        while True:
            chunk_it = itertools.islice(it, n)
            try:
                first_el = next(chunk_it)
            except StopIteration:
                return
            yield itertools.chain((first_el,), chunk_it)

if __name__ == "__main__":
    daily = CommandLine()
