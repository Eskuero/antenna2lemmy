#!/usr/bin/python
import requests
import time
import datetime
import sys
import json
import hjson
import html
import time
import threading
import curses
import logging
import io
import yt_dlp
import os

logging.basicConfig(filename='migration.log', encoding='utf-8', level=logging.INFO, filemode="w")
logger = logging.getLogger(__name__)

# Amount of time the program has been running
start_time = time.time()

# Load config
with open("config.hjson", "r") as infile:
	config = hjson.loads(infile.read())

# Expand config values
LEMMYHOST = config["lemmy-conn"]["host"]
ARCHIVEUSER = config["lemmy-conn"]["user"]
ARCHIVEUSER_PW = config["lemmy-conn"]["password"]
PROTOCOL = config["lemmy-conn"]["protocol"]
ORIGINHEADERS = {
	'User-agent': config["origin-conn"]["user-agent"]
}
BASE_API = PROTOCOL + "://" + LEMMYHOST + "/api/v3"

# Runtime options
MAXTHREADS = config["script-options"]["threads"]
MIGRATE_MEDIA = config["script-options"]["migratemedia"]

# Expand config values for this particular migration
COMMUNITY_NAME = config["lemmy-conn"]["community"]

# Load file of links provided
try:
	ORIGIN = sys.argv[1]
except IndexError:
	print("Provide a valid text file as argument to the program")
	sys.exit(0)
try:
	with open(ORIGIN, "r") as urlsfile:
		urls = urlsfile.read().splitlines()
except FileNotFoundError:
	print(f"The file {ORIGIN} does not exist")
	sys.exit(0)

# Obtain a login auth for the lemmy user
payload = {
	'username_or_email': ARCHIVEUSER,
	'password': ARCHIVEUSER_PW
}
try:
	response = requests.post(url = BASE_API + "/user/login", json = payload)
	AUTH = response.json()["jwt"]
except:
	print("Failed to authenticate: " + response.text)
	sys.exit(1)

# Curses related variables
interfacevars = {
	"migrated_posts": 0,
	"failed_posts": 0,
	"migrated_media": 0,
	"failed_media": 0,
	"error_output": ""
}

def main():
	# Get community ID because we cannot target by name in the lemmy API
	payload = {
		'auth': AUTH,
		'name': COMMUNITY_NAME
	}
	COMMUNITY_ID = requests.get(url = BASE_API + "/community", params = payload).json()["community_view"]["community"]["id"]

	# Do the migration of the posts asynchronously
	for url in urls:
		# NOTICE: Limit posting threads to not overload the instance
		while (len(threading.enumerate())) > 10:
			time.sleep(5)
		thread = threading.Thread(target = migratepost, args=(url, COMMUNITY_ID), kwargs={})
		thread.start()
		#migratepost(url, COMMUNITY_ID)

def migratepost(url, COMMUNITY_ID):
	url = url + ".json"

	# Obtain the content of the post
	try:
		response = requests.get(url = url, headers = ORIGINHEADERS)
		page = response.json()
		# Actually the post data is deeper in
		postdata = page[0]["data"]["children"][0]["data"]
	except:
		log("Unexpected data while getting post content. op: 'Migrating post', url: '" + url + "', response: '" + response.text + "'\n")
		updatecounter('failed_posts')
		return

	# If the page is a crosspost follow the link and try again
	try:
		while ("crosspost_parent" in postdata):
			# If media and not empty open the list and pick the url
			if ".redd.it/" in postdata["url_overridden_by_dest"] and len(postdata["crosspost_parent_list"]) > 0:
				redirect = postdata["crosspost_parent_list"][0]["permalink"]
			# Sometimes for some reason there's media without parent crosspost for some reason
			elif ".redd.it/" in postdata["url_overridden_by_dest"] or "reddit.com/gallery/" in postdata["url_overridden_by_dest"]:
				break
			else:
				redirect = postdata["url"]
			# Overwrite data we are using before testing again
			url = "https://www.reddit.com" + redirect  + ".json?limit=1000"
			response = requests.get(url = url, headers = ORIGINHEADERS)
			page = response.json()
			postdata = page[0]["data"]["children"][0]["data"]
	except:
		log("Unexpected data while recursing parent crosspost. op: 'Migrating post', url: '" + url + "', response: '" + response.text + "'\n")
		updatecounter('failed_posts')
		return

	# Compose the post content and attributes
	# FIXME: API doesn't allow specifying a timestamp for the post so dates are lost. Would this be even supported?
	# We could edit the timestamp directly on the db "UPDATE post SET published=timestamp WHERE id=id" but the post might have federated already and break things?
	# FIXME: Similarly we can't specify a number of upvotes, but we could set on the database the number
	payload = {
		'auth': AUTH,
		'community_id': COMMUNITY_ID,
		'name': postdata["title"],
		# If the post is not self the URL is null
		'url': None if postdata["is_self"] else postdata["url"],
		# The body always starts giving credit to the original poster
		'body': preparebody(postdata["author"], postdata["created_utc"], ""),
	}
	# If selftest, actually append the rest of the post body now
	if postdata["is_self"]:
		payload["body"] = preparebody(postdata["author"], postdata["created_utc"], postdata["selftext"])
	# Migrate pictures if asked
	if MIGRATE_MEDIA and payload["url"]:
		# Only expand that site hosted stuff
		if any(substring in payload["url"] for substring in ["i.redd.it", "v.redd.it"]):
			migration = migratemedia(payload["url"])
			# If migration of the media failed we keep the original link
			payload["url"] = payload["url"] if not migration else migration

	# Actually create the post
	try:
		response = requests.post(url = BASE_API + "/post", json = payload)
		POST_ID = response.json()["post_view"]["post"]["id"]
	except json.decoder.JSONDecodeError:
		log("Unexpected data on POST request to Lemmy. op: 'Migrating post', url: '" + url + "', response: '" + response.text + "'\n")
		updatecounter('failed_posts')
	except KeyError:
		# If we failed with anything other than rate limit skip this one.
		if response.json().get("error", "ok") != "rate_limit_error":
			log("Failed POST request to Lemmy. op: 'Migrating post', url: '" + url + "', response: '" + response.text + "'\n")
			updatecounter('failed_posts')
			return
		# If we are rate limited retry in timeouts of 30 seconds
		while (response.json().get("error", "ok") == "rate_limit_error"):
			log("Timed out on POST request to Lemmy. Waiting 30 seconds before retry.")
			time.sleep(30)
			response = requests.post(url = BASE_API + "/post", json = payload)
		# We should only be here if we didn't get an error of rate limit anymore
		POST_ID = response.json()["post_view"]["post"]["id"]
	except:
		log("Failed POST request to lemmy. op: 'Migrating post', url: '" + url + "', response: '" + response.text + "'\n")
		updatecounter('failed_posts')
		return

	# If we are here congratz, we successfully migrated a post
	interfacevars['error_output'] += "Succesful post migration to lemmy. url: '" + url + "'\n"
	updatecounter('migrated_posts')

def preparebody(author, date, content):
	# Always give credits to the original poster and jump line
	credits = ">*originally posted by /u/" + author + " on " + str(datetime.datetime.fromtimestamp(date)) + "*\n\n"

	# First escape the content received
	content = html.unescape(content)

	# Get the list indexes that have image previews
	lines = content.split("\n")
	matching = [lines.index(word) for word in lines if "preview.redd.it" in word]

	for index in matching:
		# If the url starts with [ simply add the !
		if lines[index][0] == "[":
			newstring = "!" + lines[index]
		# If we only have the url add a basic text
		elif lines[index][0:4] == "http":
			newstring = "![Image](" + lines[index] + ")"

		# If we set the script to replace image links, do so
		if MIGRATE_MEDIA and newstring:
			# Download original url
			originurl = newstring.split("(")[1].rstrip(")")
			migration = migratemedia(originurl)
			# If migration of the picture failed we keep the original link
			newstring = newstring if not migration else ("![Image](" + migration + ")")

		# Replace the original line with this.
		content = content.replace(lines[index], newstring)

	body = credits + content

	# FIXME: Body cannot be longer than 10.000k characters, alternate solution to not lose data?
	return body[0:9999]

def migratemedia(originurl):
	try:
		# Store videos temporarily
		if ("v.redd.it" in originurl):
			# Disable audio
			filename = "temp/" + str(time.time()) + ".mp4"
			yt_opts = {
				'outtmpl': filename,
				'quiet': True,
				'noprogress': True
			}
			with yt_dlp.YoutubeDL(yt_opts) as ydl:
				ydl.download([originurl])
			media = {'images[]': open(filename,'rb')}
		elif any(substring in originurl for substring in ["i.redd.it", "preview.redd.it"]):
			response = requests.get(originurl)
			media = {'images[]': io.BytesIO(response.content)}
	except:
		log("Failed downloading media. op: 'Migrating media', url: '" + originurl + "', response: '" + response.text + "'\n")
		updatecounter('failed_media')
		return False
	else:
		# For uploading the picture we need an AUTH
		cookies = {
			'jwt': AUTH
		}
		try:
			response = requests.post(url = PROTOCOL + "://" + LEMMYHOST + "/pictrs/image", cookies = cookies, files = media)
			newurl = PROTOCOL + "://" + LEMMYHOST + "/pictrs/image/" + response.json()["files"][0]["file"]
		except:
			log("Failed migrating media. op: 'Migrating media', url: '" + originurl + "', response: '" + response.text + "'\n")
			updatecounter('failed_media')
			return False
		else:
			updatecounter('migrated_media')
			# Delete temporal video from filesystem
			if "v.redd.it" in originurl:
				if os.path.exists(filename):
					os.remove(filename)
			return newurl

def log(message):
	global interfacevars
	interfacevars['error_output'] += message
	logger.error(message)

def updatecounter(target):
	global interfacevars
	interfacevars[target] += 1

def rendercurses():
	# Clear the screen
	stdscr.clear()

	# Print the updated values in the first section
	stdscr.addstr(0, 0, f"Migrating links from {ORIGIN} into community !{COMMUNITY_NAME}@{LEMMYHOST}", curses.color_pair(3))
	stdscr.hline(1, 0, curses.ACS_HLINE, screen_width)
	stdscr.addstr(2, 0, f"Posts migrated: {interfacevars['migrated_posts']}", curses.color_pair(1))
	stdscr.addstr(2, 30, f"Failed posts: {interfacevars['failed_posts']}", curses.color_pair(2))
	stdscr.addstr(3, 0, f"Media migrated: {interfacevars['migrated_media']}", curses.color_pair(1))
	stdscr.addstr(3, 30, f"Failed media: {interfacevars['failed_media']}", curses.color_pair(2))

	# Calculate the days, hours, minutes, and seconds
	current_time = time.time()
	elapsed_time = current_time - start_time
	runtime = datetime.timedelta(seconds=int(elapsed_time))
	days = runtime.days
	hours, remainder = divmod(runtime.seconds, 3600)
	minutes, seconds = divmod(remainder, 60)

	# Print the current runtime
	stdscr.addstr(4, 0, f"Runtime: {days} days, {hours} hours, {minutes} minutes, {seconds} seconds.", curses.color_pair(5))
	# Print the current total threads
	stdscr.addstr(4, 60, f"Threads: {str(len(threading.enumerate()))}", curses.color_pair(4))

	# Calculate the height for the second section
	second_section_height = screen_height - first_section_height

	# Print the updated text in the second section
	text_lines = interfacevars['error_output'].splitlines()
	max_rows = second_section_height - 2  # Leave one row for the border
	text_to_print = text_lines[-max_rows:]  # Get the last portion of the text
	for i, line in enumerate(text_to_print):
		try:
			stdscr.addstr(i + first_section_height + 1, 0, line)
		except:
			# FIXME: Don't crash if curses fails to print the line, just check the log so see the problem
			pass

	# Draw a border between the two sections
	stdscr.hline(first_section_height, 0, curses.ACS_HLINE, screen_width)

	# Refresh the screen
	stdscr.refresh()

# Initialize the curses screen
stdscr = curses.initscr()

# Disable automatic echoing of keys
curses.noecho()

# Define color pairs
curses.start_color()
curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)
curses.init_pair(3, curses.COLOR_BLUE, curses.COLOR_BLACK)
curses.init_pair(4, curses.COLOR_YELLOW, curses.COLOR_BLACK)
curses.init_pair(5, curses.COLOR_MAGENTA, curses.COLOR_BLACK)

# Calculate the height for the first section (10% of the screen height)
screen_height, screen_width = stdscr.getmaxyx()
# Define the height for the first section (4 rows)
first_section_height = 5

thread = threading.Thread(target=main, args=(), kwargs={})
thread.start()

while True:
	# If we only have one remaining thread it means we finished and thus can end
	if (len(threading.enumerate()) == 1):
		break
	rendercurses()
	# Sleep for 1 second
	time.sleep(1)

rendercurses()
stdscr.addstr(screen_height - 1, 0, "Completed migration, press any key to exit...")
stdscr.refresh()
stdscr.getch()

# End curses and return terminal to normal mode
curses.nocbreak()
stdscr.keypad(False)
curses.echo()
curses.endwin()
