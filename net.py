import asynchat
import socket
import struct
import logging
import base58

class BConnection(asynchat.async_chat):
	def __init__(self, addr, context):
		asynchat.async_chat.__init__(self)
		self.context, self.addr = context, addr
		self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
		logging.debug("connecting to address %s", addr)
		self.connect(addr)
		self.remote = None
		self.header_format = "<L12sL"
		self.header_size = struct.calcsize(self.header_format)
		self.set_terminator(self.header_size)
		self.last_seen = None
		self.incoming_handlers = {
			"version" : self.pop_version,
			"verack" : self.pop_verack,
			"addr" : self.pop_addr,
			"block" : self.pop_block,
		}
		self.incoming_handler = None
		self.reset_incoming_data()
		self.push_version()

	def unpack_incoming_header(self):
		(network, command, paylen), data = self.context.parser.unpack(self.header_format, self.incoming_data)
		self.incoming_data = data
		command = command.strip('\0')
		return network, command, paylen

	def checksum(self, data):
		h = base58.checksum(data)
		return h[:4]

	def collect_incoming_data(self, data):
		self.incoming_data += data
		self.last_seen = self.context.get_system_time()

	def reset_incoming_data(self):
		self.incoming_data = ""

	def handle_connect(self):
		logging.debug("connected to node %s", self.addr)

	def handle_expt(self):
		if not self.connected:
			logging.debug("connection refused to %s", self.addr)
		else:
			logging.exception()
		self.close()

	def found_terminator(self):
		logging.debug("received packet %s", len(self.incoming_data))
		if callable(self.incoming_handler):
			try:
				if self.incoming_handler():
					self.close()
				self.set_terminator(self.header_size)
			except:
				logging.exception("incoming handler")
				self.close()
			return
		network, command, paylen = self.unpack_incoming_header()
		if network != self.context.config["network"]:
			logging.error("received garbage from %s", connection.addr)
			self.close()
			return
		handler = self.incoming_handlers.get(command, None)
		if handler is None:
			logging.error("Unable to handle command %s (paylen=%s)", command, paylen)
			self.close()
			return
		logging.debug("received %s paylen %s", command, paylen)
		if paylen == 0:
			handler()
		else:
			self.incoming_handler = handler
			csize = 0
			if command not in ("version", "verack"):
				csize = self.context.parser.get_checksum_size()
			self.set_terminator(csize + paylen)

	def push_packet(self, command, data=""):
		size = len(data)
		header = struct.pack("<L12sL", self.context.config["network"], command, size)
		if size > 0 and command not in ("version", "verack"):
			header += self.checksum(data)
		self.push(header)
		logging.debug("push_packet header: %s (%s)", command, len(header))
		if size > 0:
			self.push(data)

	def push_version(self):
		remote = self.context.parser.pack_address(
				self.context.config["services"],
				self.addr[0], self.addr[1])
		local = self.context.parser.pack_address(
				self.context.config["services"],
				self.context.get_external_address(),
				self.context.config["local_port"])
		data = struct.pack("<LQQ26s26sQxL", 
				self.context.config["version"], 
				self.context.config["services"], 
				self.context.get_system_time(),
				remote, 
				local, 
				self.context.config["nonce"], 
				self.context.get_last_block())
		self.push_packet("version", data)

	def pop_version(self):
		if self.remote:
			return 
		self.incoming_handler = None
		result, data = self.context.parser.unpack("<LQQ26s26sQxL", self.incoming_data)
		self.incoming_data = data
		version, services, timestamp, remote, local, nonce, last = result
		logging.debug("pop_version %s %s %s %s %s", version, services, timestamp, nonce, last)
		if nonce == self.context.config["nonce"] and nonce > 1:
			logging.error("Connected to self, disconnecting")
			return True
		self.remote = {
			"version" : version,
			"services" : services,
			"timestamp" : timestamp,
			"nonce" : nonce,
			"last" : last
		}
		if version >= 209:
			self.push_verack()
		self.context.add_time_delta(0, timestamp)

	def push_verack(self):
		self.push_packet("verack")

	def pop_verack(self):
		logging.debug("pop_verack")
		self.incoming_handler = None
		#self.push_getaddr()
		self.push_getblocks([self.context.config["genesis_hash"]])

	def push_getaddr(self):
		logging.debug("push_getaddr")
		self.push_packet("getaddr")

	def pop_addr(self):
		logging.debug("pop_addr")
		self.incoming_handler = None
		checksum, data = self.incoming_data[:4], self.incoming_data[4:]
		if checksum != self.checksum(data):
			logging.error("checksum mismatch")
			return True
		logging.debug("checksum matches")

	def push_getblocks(self,hash_starts,hash_stop=None):
		if not hash_stop:
			hash_stop = "\0" * 32
		starts = "".join(hash_starts)
		vector = self.context.parser.pack_variable_int(len(hash_starts)) + starts
		data = struct.pack("<i", self.context.config["version"])
		self.push_packet("getblocks", data + vector + hash_stop)

	def pop_block(self):
		logging.debug("pop_block")
		self.incoming_handler = None
		checksum, data = self.incoming_data[:4], self.incoming_data[4:]
		real_checksum = self.checksum(data)
		logging.debug("%s %s", checksum.encode("hex_codec"), real_checksum.encode("hex_codec"))
		if checksum != real_checksum:
			logging.error("checksum mismatch")
			return True
		logging.debug("checksum matches")
		h = base58.checksum(data[:4+32+32+4+4+4])
		block, data = self.context.parser.unpack_block(data)
		logging.debug("block: %s %s", h.encode("hex_codec"), block)
		self.incoming_data = data

