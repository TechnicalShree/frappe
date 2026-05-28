import frappe
from frappe import _


class DbManager:
	def __init__(self, db):
		"""
		Pass root_conn here for access to all databases.
		"""
		if db:
			self.db = db

	def get_current_host(self):
		return self.db.sql("select user()")[0][0].split("@")[1]

	def create_user(self, user, password, host=None):
		host = host or self.get_current_host()
		password_predicate = f" IDENTIFIED BY '{password}'" if password else ""
		self.db.sql(f"CREATE USER '{user}'@'{host}'{password_predicate}")

	def delete_user(self, target, host=None):
		host = host or self.get_current_host()
		self.db.sql(f"DROP USER IF EXISTS '{target}'@'{host}'")

	def create_database(self, target):
		if target in self.get_database_list():
			self.drop_database(target)
		self.db.sql(f"CREATE DATABASE `{target}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")

	def drop_database(self, target):
		self.db.sql_ddl(f"DROP DATABASE IF EXISTS `{target}`")

	def grant_all_privileges(self, target, user, host=None):
		host = host or self.get_current_host()
		permissions = (
			(
				"SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER, "
				"CREATE TEMPORARY TABLES, CREATE VIEW, EVENT, TRIGGER, SHOW VIEW, "
				"CREATE ROUTINE, ALTER ROUTINE, EXECUTE, LOCK TABLES"
			)
			if frappe.conf.rds_db
			else "ALL PRIVILEGES"
		)
		self.db.sql(f"GRANT {permissions} ON `{target}`.* TO '{user}'@'{host}'")

	def flush_privileges(self):
		self.db.sql("FLUSH PRIVILEGES")

	def get_database_list(self):
		return self.db.sql("SHOW DATABASES", pluck=True)

	@staticmethod
	def restore_database(verbose: bool, target: str, source: str, user: str, password: str) -> None:
		"""
		Function to restore the given SQL file to the target database.
		:param target: The database to restore to.
		:param source: The SQL dump to restore
		:param user: The database username
		:param password: The database password
		:return: Nothing
		"""

		import gzip
		import os
		import subprocess
		import sys
		import time
		from shutil import which

		from frappe.database import get_command

		# Generate the restore command
		bin, args, bin_name = get_command(
			socket=frappe.conf.db_socket,
			host=frappe.conf.db_host,
			port=frappe.conf.db_port,
			user=user,
			password=password,
			db_name=target,
		)
		if not bin:
			return frappe.throw(
				_("{} not found in PATH! This is required to restore the database.").format(bin_name),
				exc=frappe.ExecutableNotFound,
			)

		sed = which("sed")
		if not sed:
			return frappe.throw(
				_("{} not found in PATH! This is required to restore the database.").format("sed"),
				exc=frappe.ExecutableNotFound,
			)

		source_size = os.path.getsize(source)
		start = time.monotonic()
		last_update = 0
		last_percent = -1
		spinner = ("-", "\\", "|", "/")
		spinner_index = 0

		def show_progress(bytes_read: int, done: bool = False) -> None:
			nonlocal last_update, last_percent, spinner_index
			if not source_size:
				return

			now = time.monotonic()
			percent = min(100, int(bytes_read * 100 / source_size))
			if not done and percent == last_percent and now - last_update < 0.5:
				return

			last_update = now
			last_percent = percent
			elapsed = max(now - start, 0.001)
			rate = bytes_read / elapsed
			remaining = max(source_size - bytes_read, 0)
			eta = int(remaining / rate) if rate else 0

			def format_duration(seconds: int) -> str:
				if seconds >= 3600:
					return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
				if seconds >= 60:
					return f"{seconds // 60}m{seconds % 60:02d}s"
				return f"{seconds}s"

			terminal_width = max(60, os.get_terminal_size().columns if sys.stderr.isatty() else 80)
			prefix = "Restore DB"
			stats = (
				f"{percent:3d}% "
				f"{bytes_read / 1024 / 1024:.0f}/{source_size / 1024 / 1024:.0f}MB "
				f"{rate / 1024 / 1024:.1f}MB/s "
				f"ETA {format_duration(eta)}"
			)
			bar_width = max(10, terminal_width - len(prefix) - len(stats) - 8)
			filled = min(bar_width, int(bar_width * percent / 100))
			bar = "#" * filled + "-" * (bar_width - filled)
			indicator = "done" if done else spinner[spinner_index % len(spinner)]
			spinner_index += 1
			message = f"{prefix} {indicator} [{bar}] {stats}"
			sys.stderr.write(f"\r\033[2K{message[: terminal_width - 1]}")
			sys.stderr.flush()
			if done:
				sys.stderr.write("\n")
				sys.stderr.flush()

		sed_sandbox = subprocess.Popen(
			[sed, r"/\/\*M\{0,1\}!999999\\- enable the sandbox mode \*\//d"],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
		)
		sed_definer = subprocess.Popen(
			[sed, r"/\/\*![0-9]* DEFINER=[^ ]* SQL SECURITY DEFINER \*\//d"],
			stdin=sed_sandbox.stdout,
			stdout=subprocess.PIPE,
		)
		db_import = subprocess.Popen(
			[bin, *args],
			stdin=sed_definer.stdout,
			stdout=None if verbose else subprocess.DEVNULL,
			stderr=None,
		)

		assert sed_sandbox.stdin
		assert sed_sandbox.stdout
		assert sed_definer.stdout
		sed_sandbox.stdout.close()
		sed_definer.stdout.close()

		bytes_read = 0
		try:
			fileobj = open(source, "rb")
			reader = gzip.GzipFile(fileobj=fileobj) if source.endswith(".gz") else fileobj
			with fileobj, reader:
				while chunk := reader.read(1024 * 1024):
					sed_sandbox.stdin.write(chunk)
					bytes_read = fileobj.tell() if source.endswith(".gz") else bytes_read + len(chunk)
					show_progress(bytes_read)
			sed_sandbox.stdin.close()
		except BrokenPipeError:
			pass
		finally:
			if sed_sandbox.stdin and not sed_sandbox.stdin.closed:
				sed_sandbox.stdin.close()

		return_codes = {
			"sed sandbox filter": sed_sandbox.wait(),
			"sed definer filter": sed_definer.wait(),
			bin_name: db_import.wait(),
		}
		show_progress(source_size, done=True)

		for process_name, return_code in return_codes.items():
			if return_code:
				raise subprocess.CalledProcessError(return_code, process_name)

		frappe.cache.delete_keys("")  # Delete all keys associated with this site.
