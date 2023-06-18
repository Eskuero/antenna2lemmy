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

DEBUGMODE = True if os.environ.get("DEBUGMODE", 0) == "1" else False

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
THREADING = config["script-options"]["threading"]
MAXTHREADS = config["script-options"]["max_threads"]

# Decisions of media migration
MIGRATE_PICTURES = config["script-options"]["migrateimages"]
MIGRATE_VIDEOS = config["script-options"]["migratevideos"]
MEDIA_SKIP_ON_FAIL = config["script-options"]["media_skip_on_fail"]

# Decisions of comment migration
MIGRATE_COMMENTS = config["script-options"]["migratecomments"]

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

# Get community ID because we cannot target by name in API
payload = {
	'auth': AUTH,
	'name': COMMUNITY_NAME
}
try:
	COMMUNITY_ID = requests.get(url = BASE_API + "/community", params = payload).json()["community_view"]["community"]["id"]
except:
	print("Failed to get community ID for " + COMMUNITY_NAME + ", are you sure it exists?")
	sys.exit(1)

def main():
	# Do the migration of the posts asynchronously
	for url in urls:
		# NOTICE: Limit posting threads to not overload the instance
		while (len(threading.enumerate())) > MAXTHREADS:
			time.sleep(5)
		if not DEBUGMODE and THREADING:
			thread = threading.Thread(target = migratepost, args=(url, COMMUNITY_ID), kwargs={})
			thread.start()
		else:
			migratepost(url, COMMUNITY_ID)

def migratepost(url, COMMUNITY_ID):
	url = url + ".json"

	# Obtain the content of the post
	try:
		response = requests.get(url = url, headers = ORIGINHEADERS)
		page = response.json()
		# Actually the post data is deeper in
		postdata = page[0]["data"]["children"][0]["data"]
	except:
		log("Unexpected data. op: 'Downloading post', url: '" + url + "', response: '" + response.text, "error")
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
		log("Unexpected data. op: 'Recursing crosspost', url: '" + url + "', response: '" + response.text, "error")
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
		'body': ""
	}

	# Migrate media if asked with a proper link
	if payload["url"]:
		# Only expand that site hosted stuff
		if (MIGRATE_PICTURES and "i.redd.it" in payload["url"]) or (MIGRATE_VIDEOS and "v.redd.it" in payload["url"]):
			migration = migratemedia(payload["url"])
			if migration:
				payload["url"] = migration
			# If migration of the media failed decide whether to skip this post or keep the old link
			elif MEDIA_SKIP_ON_FAIL:
				log("Failed. op: 'Migrating post', url: '" + url + "', response: 'Mandated to skip because migration of " + payload["url"] + " failed'", "error")
				return
			else:
				log("Ignoring failure. op: 'Migrating post', url: '" + url + "', response: 'Mandated to continue despite migration of " + payload["url"] + " failing'", "warning")

	# If selftest, actually append the rest of the post body now doing some cleanups and migrating inline images
	credits = (postdata["author"], postdata["created_utc"])
	result, payload["body"] = preparebody(credits, postdata["selftext"]) if postdata["is_self"] else preparebody(credits, "")
	# A failed report means we are skipping the post because mediadidn't went through
	if result == "failed":
		log("Failed. op: 'Migrating post', url: '" + url + "', response: 'Mandated to skip because migration of inline image failed'", "error")
		return;
	elif result == "ignore":
		log("Ignoring failure. op: 'Migrating post', url: '" + url + "', response: 'Mandated to continue despite migration of inline image failing'", "warning")

	# Actually create the post
	try:
		response = requests.post(url = BASE_API + "/post", json = payload)
		POST_ID = response.json()["post_view"]["post"]["id"]
	except json.decoder.JSONDecodeError:
		log("Unexpected data. op: 'Migrating post', url: '" + url + "', response: '" + response.text, "error")
		updatecounter('failed_posts')
	except KeyError:
		# If we failed with anything other than rate limit skip this one.
		if response.json().get("error", "ok") != "rate_limit_error":
			log("Failed. op: 'Migrating post', url: '" + url + "', response: '" + response.text, "error")
			updatecounter('failed_posts')
			return
		# If we are rate limited retry in timeouts of 30 seconds
		while (response.json().get("error", "ok") == "rate_limit_error"):
			log("Timed out and waiting 30 seconds. op: 'Migrating post', url: '" + url + "', response: '" + response.text, "warning")
			time.sleep(30)
			response = requests.post(url = BASE_API + "/post", json = payload)
		# We should only be here if we didn't get an error of rate limit anymore
		POST_ID = response.json()["post_view"]["post"]["id"]
	except:
		log("Failed. op: 'Migrating post', url: '" + url + "', response: '" + response.text, "error")
		updatecounter('failed_posts')
		return

	# If we are here congratz, we successfully migrated a post
	log("Successful. op: 'Migrating post', url: '" + url, "info")
	updatecounter('migrated_posts')

	# Get the base list of comments and restore them if enabled
	if MIGRATE_COMMENTS:
		comments = page[1]["data"]["children"]
		# Transverse the entire replies section adding everything with their corresponding parent
		migratecomments(comments, BASE_API, AUTH, POST_ID)

def migratecomments(comments, BASE_API, AUTH, POST_ID, PARENT_ID = None):
	# Loop through each child comment
	for comment in [comment["data"] for comment in comments if comment["kind"] != "more"]:
		# Compose the comment content and attributes
		try:
			payload = {
				'auth': AUTH,
				'post_id': POST_ID,
				'content': "",
				"parent_id": PARENT_ID
			}
			result, payload['content'] = preparebody((comment["author"], comment["created_utc"]), comment["body"])
		# If we failed to parse this comment simply move on
		except:
			log("Failed. op: 'Parsing comment', POST_ID: '" + str(POST_ID) + "', PARENT_ID: '" + str(PARENT_ID) + "', comment: '" + str(comment), "error")
			updatecounter('failed_comments')
			continue

		# Actually create the comment and retrieve de post id
		try:
			response = requests.post(url = BASE_API + "/comment", json = payload)
			COMMENT_ID = response.json()["comment_view"]["comment"]["id"]
		# Received an invalid response that doesn't parse as Json'
		except json.decoder.JSONDecodeError:
			log("Failed. op: 'Migrating comment', POST_ID: '" + str(POST_ID) + "', PARENT_ID: '" + str(PARENT_ID) + "', response: '" + response.text + "', payload: " + str(payload), "error")
			updatecounter('failed_comments')
			continue
		except KeyError:
			# If we failed with anything other than rate limit skip this one.
			if response.json().get("error", "ok") != "rate_limit_error":
				log("Failed. op: 'Migrating comment', POST_ID: '" + str(POST_ID) + "', PARENT_ID: '" + str(PARENT_ID) + "', response: '" + response.text + "', payload: " + str(payload), "error")
				updatecounter('failed_comments')
				continue
			# If we are rate limited retry in timeouts of 30 seconds
			while (response.json().get("error", "ok") == "rate_limit_error"):
				log("Timed out on POST request to Lemmy. Waiting 30 seconds before retry.")
				log("Timed out and waiting 30 seconds. op: 'Migrating comment', POST_ID: '" + str(POST_ID) + "', PARENT_ID: '" + str(PARENT_ID) + "', payload: " + str(payload), "warning")
				time.sleep(30)
				try:
					response = requests.post(url = BASE_API + "/comment", json = payload)
					COMMENT_ID = response.json()["comment_view"]["comment"]["id"]
				# If we failed without rate limit again we break this loop and stop trying
				except KeyError:
					if response.json().get("error", "ok") != "rate_limit_error":
						log("Failed. op: 'Migrating comment', POST_ID: '" + str(POST_ID) + "', PARENT_ID: '" + str(PARENT_ID) + "', response: '" + response.text + "', payload: " + str(payload), "error")
						updatecounter('failed_comments')
						break
				except:
					log("Failed. op: 'Migrating comment', POST_ID: '" + str(POST_ID) + "', PARENT_ID: '" + str(PARENT_ID) + "', response: '" + response.text + "', payload: " + str(payload), "error")
					updatecounter('failed_comments')
					break
		except:
			log("Failed. op: 'Migrating comment', POST_ID: '" + str(POST_ID) + "', PARENT_ID: '" + str(PARENT_ID) + "', response: '" + response.text + "', payload: " + str(payload), "error")
			updatecounter('failed_comments')
			continue

		# If we arrived here it means the comment succesfully migrated
		updatecounter('migrated_comments')

		# Recurse again if the comment has more children replies
		if comment["replies"]:
			# NOTICE: DO NOT ENABLE THREADING FOR COMMENTS YOU MIGHT CRASH YOUR INSTANCE
			# thread = threading.Thread(target = migratecomments, args=(comment["replies"]["data"]["children"], BASE_API, AUTH, POST_ID, COMMENT_ID), kwargs={})
			# thread.start()
			migratecomments(comment["replies"]["data"]["children"], BASE_API, AUTH, POST_ID, COMMENT_ID)

def preparebody(credits, content):
	# Always give credits to the original poster and jump line
	credits = ">*originally posted by /u/" + credits[0] + " on " + str(datetime.datetime.fromtimestamp(credits[1])) + "*\n\n"

	# To know how preparation went
	status = "correct"

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
		if MIGRATE_PICTURES and newstring:
			# Download original url
			originurl = newstring.split("(")[1].rstrip(")")
			migration = migratemedia(originurl)
			if migration:
				newstring = newstring if not migration else ("![Image](" + migration + ")")
			# If migration of the media failed decide whether to skip this post or keep the old link
			elif MEDIA_SKIP_ON_FAIL:
				return "failed", ""
			else:
				status = "ignore"

		# Replace the original line with this.
		content = content.replace(lines[index], newstring)

	body = status, credits + content

	# FIXME: Body cannot be longer than 10.000k characters, alternate solution to not lose data?
	return body[0:9999]

def migratemedia(originurl):
	try:
		# Store videos temporarily
		if ("v.redd.it" in originurl):
			# Disable audio
			filename = "temp/" + originurl.replace("/","") + ".mp4"
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
		# FIXME: Proper error reporting from yt-dlp?
		log("Failed. op: 'Downloading media', url: '" + originurl + "'", "error")
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
		except json.decoder.JSONDecodeError:
			log("Failed. op: 'Migrating media', url: '" + originurl + "', response: '" + response.text, "error")
			updatecounter('failed_media')
			return False
		except KeyError:
			# If we failed with anything other than rate limit skip this one.
			if response.json().get("error", "ok") != "rate_limit_error":
				log("Failed. op: 'Migrating media', url: '" + originurl + "', response: '" + response.text, "error")
				updatecounter('failed_media')
				return False
			# If we are rate limited retry in timeouts of 30 seconds
			while (response.json().get("error", "ok") == "rate_limit_error"):
				log("Timed out and waiting 30 seconds. op: 'Migrating media', url: '" + originurl + "', response: '" + response.text, "warning")
				time.sleep(30)
				try:
					response = requests.post(url = PROTOCOL + "://" + LEMMYHOST + "/pictrs/image", cookies = cookies, files = media)
					newurl = PROTOCOL + "://" + LEMMYHOST + "/pictrs/image/" + response.json()["files"][0]["file"]
				# If we failed without rate limit again we break this loop and stop trying
				except KeyError:
					if response.json().get("error", "ok") != "rate_limit_error":
						log("Failed. op: 'Migrating media', url: '" + originurl + "', response: '" + response.text, "error")
						interfacevars['failed_media'] += 1
						return False
				except:
					log("Failed. op: 'Migrating media', url: '" + originurl + "', response: '" + response.text, "error")
					interfacevars['failed_media'] += 1
					return False
		except:
			log("Failed. op: 'Migrating media', url: '" + originurl + "', response: '" + response.text, "error")
			updatecounter('failed_media')
			return
		else:
			updatecounter('migrated_media')
			# Delete temporal video from filesystem
			if "v.redd.it" in originurl:
				if os.path.exists(filename):
					os.remove(filename)
			return newurl

def log(message, level):
	if DEBUGMODE:
		print(message)
	else:
		global interfacevars
		# Save for curses output, remove all newlines and carriage returns that blow it up beforehand
		interfacevars['error_output'].append(message.replace("\r\n",""))
	match level:
		case "error":
			logger.error(message + "\n")
		case "warning":
			logger.warning(message + "\n")
		case "info":
			logger.info(message + "\n")

def updatecounter(target):
	if not DEBUGMODE:
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
	stdscr.addstr(4, 0, f"Comments migrated: {interfacevars['migrated_comments']}", curses.color_pair(1))
	stdscr.addstr(4, 30, f"Failed comments: {interfacevars['failed_comments']}", curses.color_pair(2))

	# Calculate the days, hours, minutes, and seconds
	current_time = time.time()
	elapsed_time = current_time - start_time
	runtime = datetime.timedelta(seconds=int(elapsed_time))
	days = runtime.days
	hours, remainder = divmod(runtime.seconds, 3600)
	minutes, seconds = divmod(remainder, 60)

	# Print the current runtime
	stdscr.addstr(5, 0, f"Runtime: {days} days, {hours} hours, {minutes} minutes, {seconds} seconds.", curses.color_pair(5))
	# Print the current total threads
	stdscr.addstr(5, 60, f"Threads: {str(len(threading.enumerate()))}", curses.color_pair(4))

	# Calculate the height for the second section
	second_section_height = screen_height - first_section_height

	# Print the updated text in the second section
	max_rows = second_section_height - 2  # Leave one row for the border
	text_to_print = interfacevars['error_output'][-max_rows:]  # Get the last portion of the text
	for i, line in enumerate(text_to_print):
		try:
			match line[:6]:
				case "Succes":
					stdscr.addstr(i + first_section_height + 1, 0, line[:screen_width], curses.color_pair(1))
				case "Failed":
					stdscr.addstr(i + first_section_height + 1, 0, line[:screen_width], curses.color_pair(2))
				case "Unexpe":
					stdscr.addstr(i + first_section_height + 1, 0, line[:screen_width], curses.color_pair(2))
				case "Timed ":
					stdscr.addstr(i + first_section_height + 1, 0, line[:screen_width], curses.color_pair(4))
				case "Ignori":
					stdscr.addstr(i + first_section_height + 1, 0, line[:screen_width], curses.color_pair(4))
		except:
			# FIXME: Don't crash if curses fails to print the line, just check the log so see the problem
			pass

	# Draw a border between the two sections
	stdscr.hline(first_section_height, 0, curses.ACS_HLINE, screen_width)

	# Refresh the screen
	stdscr.refresh()

if DEBUGMODE:
	main()
else:
	# Curses related variables
	interfacevars = {
		"migrated_posts": 0,
		"failed_posts": 0,
		"migrated_media": 0,
		"failed_media": 0,
		"migrated_comments": 0,
		"failed_comments": 0,
		"error_output": []
	}
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
	first_section_height = 6

	# Start the migration
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
