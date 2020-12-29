import sys
import time
import traceback

from common.constants import privileges
from common.log import logUtils as log
from common.ripple import userUtils
from constants import exceptions
from constants import serverPackets
from helpers import aobaHelper
from helpers import chatHelper as chat
from helpers import countryHelper
from helpers import locationHelper
from helpers import kotrikhelper
from objects import glob
from datetime import datetime
from objects import glob

import random

def handle(tornadoRequest):
	# Data to return
	responseToken = None
	responseTokenString = "ayy"
	responseData = bytes()

	# Get IP from tornado request
	requestIP = tornadoRequest.getRequestIP()

	# Avoid exceptions
	clientData = ["unknown", "unknown", "unknown", "unknown", "unknown"]
	osuVersion = "unknown"

	# Split POST body so we can get username/password/hardware data
	# 2:-3 thing is because requestData has some escape stuff that we don't need
	loginData = str(tornadoRequest.request.body)[2:-3].split("\\n")
	try:
		# Make sure loginData is valid
		if len(loginData) < 3:
			raise exceptions.invalidArgumentsException()

		# Get HWID, MAC address and more
		# Structure (new line = "|", already split)
		# [0] osu! version
		# [1] plain mac addressed, separated by "."
		# [2] mac addresses hash set
		# [3] unique ID
		# [4] disk ID
		splitData = loginData[2].split("|")
		osuVersion = splitData[0]
		timeOffset = int(splitData[1])
		clientData = splitData[3].split(":")[:5]
		if len(clientData) < 4:
			raise exceptions.forceUpdateException()

		# Try to get the ID from username
		username = str(loginData[0])
		userID = userUtils.getID(username)

		if not userID:
			# Invalid username
			raise exceptions.loginFailedException()
		if not userUtils.checkLogin(userID, loginData[1]):
			# Invalid password
			raise exceptions.loginFailedException()

		# Make sure we are not banned or locked
		priv = userUtils.getPrivileges(userID)
		if userUtils.isBanned(userID) and priv & privileges.USER_PENDING_VERIFICATION == 0:
			raise exceptions.loginBannedException()
		if userUtils.isLocked(userID) and priv & privileges.USER_PENDING_VERIFICATION == 0:
			raise exceptions.loginLockedException()

		# 2FA check
		if userUtils.check2FA(userID, requestIP):
			log.warning("Need 2FA check for user {}".format(loginData[0]))
			raise exceptions.need2FAException()

		# No login errors!

		# Verify this user (if pending activation)
		firstLogin = False
		if priv & privileges.USER_PENDING_VERIFICATION > 0 or not userUtils.hasVerifiedHardware(userID):
			if userUtils.verifyUser(userID, clientData):
				# Valid account
				log.info("Account {} verified successfully!".format(userID))
				glob.verifiedCache[str(userID)] = 1
				firstLogin = True
			else:
				# Multiaccount detected
				log.info("Account {} NOT verified!".format(userID))
				glob.verifiedCache[str(userID)] = 0
				raise exceptions.loginBannedException()


		# Save HWID in db for multiaccount detection
		hwAllowed = userUtils.logHardware(userID, clientData, firstLogin)

		# This is false only if HWID is empty
		# if HWID is banned, we get restricted so there's no
		# need to deny bancho access
		if not hwAllowed:
			raise exceptions.haxException()

		# Log user IP
		userUtils.logIP(userID, requestIP)

		# Log user osuver
		kotrikhelper.setUserLastOsuVer(userID, osuVersion)

		# Delete old tokens for that user and generate a new one
		isTournament = "tourney" in osuVersion
		if not isTournament:
			glob.tokens.deleteOldTokens(userID)
		responseToken = glob.tokens.addToken(userID, requestIP, timeOffset=timeOffset, tournament=isTournament)
		responseTokenString = responseToken.token

		# Check restricted mode (and eventually send message)
		responseToken.checkRestricted()

		# Check if frozen
		IsFrozen = glob.db.fetch(f"SELECT frozen, firstloginafterfrozen, freezedate FROM users WHERE id = {userID} LIMIT 1") #ok kids, dont ever use formats in sql queries. here i can do it as the userID comes from a trusted source (this being pep.py itself) so it wont leave me susceptable to sql injection
		frozen = bool(IsFrozen["frozen"])

		present = datetime.now()
		readabledate = datetime.utcfromtimestamp(IsFrozen["freezedate"]).strftime('%d-%m-%Y %H:%M:%S')
		date2 = datetime.utcfromtimestamp(IsFrozen["freezedate"]).strftime('%d/%m/%Y')
		date3 = present.strftime('%d/%m/%Y')
		passed = date2 < date3
		if frozen and passed == False:
				responseToken.enqueue(serverPackets.notification(f"The osuHOW staff team has found you suspicious and would like to request a liveplay. You have until {readabledate} (UTC) to provide a liveplay to the staff team. This can be done via the osuHOW Discord server. Failure to provide a valid liveplay will result in your account being automatically restricted."))
		elif frozen and passed == True:
				responseToken.enqueue(serverPackets.notification("Your window for liveplay sumbission has expired! Your account has been restricted as per our cheating policy. Please contact staff for more information on what can be done. This can be done via the osuHOW Discord server."))
				userUtils.restrict(responseToken.userID)

		#we thank unfrozen people
		first = IsFrozen["firstloginafterfrozen"]
		
		if not frozen and first:
			responseToken.enqueue(serverPackets.notification("Thank you for providing a liveplay! You have proven your legitemacy and have subsequently been unfrozen."))
			glob.db.execute(f"UPDATE users SET firstloginafterfrozen = 0 WHERE id = {userID}")

		# Deprecate telegram 2fa and send alert
		#if userUtils.deprecateTelegram2Fa(userID):
		#	responseToken.enqueue(serverPackets.notification("As stated on our blog, Telegram 2FA has been deprecated on 29th June 2018. Telegram 2FA has just been disabled from your account. If you want to keep your account secure with 2FA, please enable TOTP-based 2FA from our website https://ripple.moe. Thank you for your patience."))

		# Set silence end UNIX time in token
		responseToken.silenceEndTime = userUtils.getSilenceEnd(userID)

		# Get only silence remaining seconds
		silenceSeconds = responseToken.getSilenceSecondsLeft()

		# Get supporter/GMT
		userGMT = False
		if not userUtils.isRestricted(userID):
			userSupporter = True
		else:
			userSupporter = False
		userTournament = False
		if responseToken.admin:
			userGMT = True
		if responseToken.privileges & privileges.USER_TOURNAMENT_STAFF > 0:
			userTournament = True

		# Server restarting check
		if glob.restarting:
			raise exceptions.banchoRestartingException()

		# Send login notification before maintenance message
		#if glob.banchoConf.config["loginNotification"] != "":

		#creating notification
		OnlineUsers = int(glob.redis.get("ripple:online_users").decode("utf-8"))
		Notif = f"""- Online Users: {OnlineUsers}
		- {random.choice(glob.banchoConf.config['Quotes'])}"""
		responseToken.enqueue(serverPackets.notification(Notif))

		# Maintenance check
		if glob.banchoConf.config["banchoMaintenance"]:
			if not userGMT:
				# We are not mod/admin, delete token, send notification and logout
				glob.tokens.deleteToken(responseTokenString)
				raise exceptions.banchoMaintenanceException()
			else:
				# We are mod/admin, send warning notification and continue
				responseToken.enqueue(serverPackets.notification("Bancho is in maintenance mode. Only mods/admins have full access to the server.\nType !system maintenance off in chat to turn off maintenance mode."))



		# BAN CUSTOM CHEAT CLIENTS
		# 0Ainu = First Ainu build
		# b20190326.2 = Ainu build 2 (MPGH PAGE 10)
		# b20190401.22f56c084ba339eefd9c7ca4335e246f80 = Ainu Aoba's Birthday Build
		# b20191223.3 = Unknown Ainu build? (Taken from most users osuver in cookiezi.pw)
		# b20190226.2 = hqOsu (hq-af)
		if glob.conf.extra["mode"]["anticheat"]:
			# Ainu Client 2020 update
			if tornadoRequest.request.headers.get("ainu") == "happy":
				log.info(f"Account {userID} tried to use Ainu Client 2020!")
				if userUtils.isRestricted(userID):
					responseToken.enqueue(serverPackets.notification("Ainu client... Really? Welp enjoy your ban!"))
				else:
					glob.tokens.deleteToken(userID)
					userUtils.restrict(userID)
					raise exceptions.loginCheatClientsException()
			# Ainu Client 2019
			elif aobaHelper.getOsuVer(userID) in ["0Ainu", "b20190326.2", "b20190401.22f56c084ba339eefd9c7ca4335e246f80", "b20191223.3"]:
				log.info(f"Account {userID} tried to use Ainu Client!")
				if userUtils.isRestricted(userID):
					responseToken.enqueue(serverPackets.notification("Ainu client... Really? Welp enjoy your ban!"))
				else:
					glob.tokens.deleteToken(userID)
					userUtils.restrict(userID)
					raise exceptions.loginCheatClientsException()
			# hqOsu
			elif aobaHelper.getOsuVer(userID) == "b20190226.2":
				log.info(f"Account {userID} tried to use hqOsu!")
				if userUtils.isRestricted(userID):
					responseToken.enqueue(serverPackets.notification("Trying to use hqOsu in here? lol bye."))
				else:
					glob.tokens.deleteToken(userID)
					userUtils.restrict(userID)
					raise exceptions.loginCheatClientsException()
			
			#hqosu legacy
			elif aobaHelper.getOsuVer(userID) == "b20190716.5":
				log.info(f"Account {userID} tried to use hqOsu legacy!")
				if userUtils.isRestricted(userID):
					responseToken.enqueue(serverPackets.notification("Trying to play with HQOsu Legacy? Cute..."))
				else:
					glob.tokens.deleteToken(userID)
					userUtils.restrict(userID)
					raise exceptions.loginCheatClientsException()

		# Send all needed login packets
		responseToken.enqueue(serverPackets.silenceEndTime(silenceSeconds))
		responseToken.enqueue(serverPackets.userID(userID))
		responseToken.enqueue(serverPackets.protocolVersion())
		responseToken.enqueue(serverPackets.userSupporterGMT(userSupporter, userGMT, userTournament))
		responseToken.enqueue(serverPackets.userPanel(userID, True))
		responseToken.enqueue(serverPackets.userStats(userID, True))

		# Channel info end (before starting!?! wtf bancho?)
		responseToken.enqueue(serverPackets.channelInfoEnd())
		# Default opened channels
		# TODO: Configurable default channels
		chat.joinChannel(token=responseToken, channel="#osu")
		chat.joinChannel(token=responseToken, channel="#announce")

		# Join admin channel if we are an admin
		if responseToken.admin:
			chat.joinChannel(token=responseToken, channel="#admin")

		# Output channels info
		for key, value in glob.channels.channels.items():
			if value.publicRead and not value.hidden:
				responseToken.enqueue(serverPackets.channelInfo(key))

		# Send friends list
		responseToken.enqueue(serverPackets.friendList(userID))

		# Send main menu icon
		if glob.banchoConf.config["menuIcon"] != "":
			responseToken.enqueue(serverPackets.mainMenuIcon(glob.banchoConf.config["menuIcon"]))

		# Send online users' panels
		with glob.tokens:
			for _, token in glob.tokens.tokens.items():
				if not token.restricted:
					responseToken.enqueue(serverPackets.userPanel(token.userID))

		# Get location and country from ip.zxq.co or database
		if glob.localize:
			# Get location and country from IP
			latitude, longitude = locationHelper.getLocation(requestIP)
			countryLetters = locationHelper.getCountry(requestIP)
			country = countryHelper.getCountryID(countryLetters)
		else:
			# Set location to 0,0 and get country from db
			log.warning("Location skipped")
			latitude = 0
			longitude = 0
			countryLetters = "XX"
			country = countryHelper.getCountryID(userUtils.getCountry(userID))

		# Set location and country
		responseToken.setLocation(latitude, longitude)
		responseToken.country = country

		# Set country in db if user has no country (first bancho login)
		if userUtils.getCountry(userID) == "XX":
			userUtils.setCountry(userID, countryLetters)

		# Send to everyone our userpanel if we are not restricted or tournament
		if not responseToken.restricted:
			glob.streams.broadcast("main", serverPackets.userPanel(userID))

		# Set reponse data to right value and reset our queue
		responseData = responseToken.queue
		responseToken.resetQueue()
	except exceptions.loginFailedException:
		# Login failed error packet
		# (we don't use enqueue because we don't have a token since login has failed)
		responseData += serverPackets.loginFailed()
	except exceptions.invalidArgumentsException:
		# Invalid POST data
		# (we don't use enqueue because we don't have a token since login has failed)
		responseData += serverPackets.loginFailed()
		responseData += serverPackets.notification("I see what you're doing...")
	except exceptions.loginBannedException:
		# Login banned error packet
		responseData += serverPackets.loginBanned()
	except exceptions.loginLockedException:
		# Login banned error packet
		responseData += serverPackets.loginLocked()
	except exceptions.loginCheatClientsException:
		# Banned for logging in with cheats
		responseData += serverPackets.loginCheats()
	except exceptions.banchoMaintenanceException:
		# Bancho is in maintenance mode
		responseData = bytes()
		if responseToken is not None:
			responseData = responseToken.queue
		responseData += serverPackets.notification("Our bancho server is in maintenance mode. Please try to login again later.")
		responseData += serverPackets.loginFailed()
	except exceptions.banchoRestartingException:
		# Bancho is restarting
		responseData += serverPackets.notification("Bancho is restarting. Try again in a few minutes.")
		responseData += serverPackets.loginFailed()
	except exceptions.need2FAException:
		# User tried to log in from unknown IP
		responseData += serverPackets.needVerification()
	except exceptions.haxException:
		# Using oldoldold client, we don't have client data. Force update.
		# (we don't use enqueue because we don't have a token since login has failed)
		responseData += serverPackets.forceUpdate()
		responseData += serverPackets.notification("stop no plz update lol")
	except:
		log.error("Unknown error!\n```\n{}\n{}```".format(sys.exc_info(), traceback.format_exc()))
	finally:
		# Console and discord log
		if len(loginData) < 3:
			log.info("Invalid bancho login request from **{}** (insufficient POST data)".format(requestIP), "bunker")

		# Return token string and data
		return responseTokenString, responseData
