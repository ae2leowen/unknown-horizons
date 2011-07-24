# ###################################################
# Copyright (C) 2011 The Unknown Horizons Team
# team@unknown-horizons.org
# This file is part of Unknown Horizons.
#
# Unknown Horizons is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the
# Free Software Foundation, Inc.,
# 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
# ###################################################

import logging

from collections import deque, defaultdict

from mission.foundsettlement import FoundSettlement
from mission.preparefoundationship import PrepareFoundationShip
from mission.domestictrade import DomesticTrade
from mission.internationaltrade import InternationalTrade
from landmanager import LandManager
from completeinventory import CompleteInventory
from settlementmanager import SettlementManager
from unitbuilder import UnitBuilder
from constants import BUILDING_PURPOSE

# all subclasses of AbstractBuilding have to be imported here to register the available buildings
from building import AbstractBuilding
from building.farm import AbstractFarm
from building.field import AbstractField
from building.weaver import AbstractWeaver
from building.distillery import AbstractDistillery
from building.villagebuilding import AbstractVillageBuilding
from building.claydeposit import AbstractClayDeposit
from building.claypit import AbstractClayPit
from building.brickyard import AbstractBrickyard
from building.fishdeposit import AbstractFishDeposit
from building.fisher import AbstractFisher
from building.tree import AbstractTree
from building.lumberjack import AbstractLumberjack
from building.irondeposit import AbstractIronDeposit
from building.ironmine import AbstractIronMine
from building.charcoalburner import AbstractCharcoalBurner
from building.smeltery import AbstractSmeltery
from building.toolmaker import AbstractToolmaker
from building.boatbuilder import AbstractBoatBuilder
from building.signalfire import AbstractSignalFire

from horizons.scheduler import Scheduler
from horizons.util import Callback, WorldObject
from horizons.constants import RES, BUILDINGS, TRADER
from horizons.ext.enum import Enum
from horizons.ai.generic import GenericAI
from horizons.util.python import decorators

class AIPlayer(GenericAI):
	"""This is the AI that builds settlements."""

	shipStates = Enum.get_extended(GenericAI.shipStates, 'on_a_mission')

	log = logging.getLogger("ai.aiplayer")

	def __init__(self, session, id, name, color, **kwargs):
		super(AIPlayer, self).__init__(session, id, name, color, **kwargs)
		self.need_more_ships = False
		self._need_feeder_island = False
		self.__init()
		Scheduler().add_new_object(Callback(self.finish_init), self, run_in = 0)
		Scheduler().add_new_object(Callback(self.tick), self, run_in = 2)

	def get_available_islands(self, min_land):
		options = []
		for island in self.session.world.islands:
			if island.worldid in self.islands:
				continue

			flat_land = 0
			for tile in island.ground_map.itervalues():
				if 'constructible' not in tile.classes:
					continue
				if tile.object is not None and not tile.object.buildable_upon:
					continue
				if tile.settlement is not None:
					continue
				flat_land += 1
			if flat_land >= min_land:
				options.append((flat_land, island))
		return options

	def choose_island(self, min_land):
		options = self.get_available_islands(min_land)
		if not options:
			return None
		total_land = sum(zip(*options)[0])

		# choose a random big enough island with probability proportional to the available land
		choice = self.session.random.randint(0, total_land - 1)
		for (land, island) in options:
			if choice <= land:
				return island
			choice -= land
		return None

	def finish_init(self):
		for ship in self.session.world.ships:
			if ship.owner == self and ship.is_selectable:
				self.ships[ship] = self.shipStates.idle

	def refresh_ships(self):
		""" called when a new ship is added to the fleet """
		for ship in self.session.world.ships:
			if ship.owner == self and ship.is_selectable and ship not in self.ships:
				self.log.info('%s Added %s to the fleet', self, ship)
				self.ships[ship] = self.shipStates.idle
		self.need_more_ships = False

	def __init(self):
		self.world = self.session.world
		self.islands = {}
		self.settlement_managers = []
		self._settlement_manager_by_settlement_id = {}
		self.missions = set()
		self.fishers = []
		self.complete_inventory = CompleteInventory(self)
		self.unit_builder = UnitBuilder(self)
		self.settlement_expansions = [] # [(coords, settlement)]

	def report_success(self, mission, msg):
		self.missions.remove(mission)
		if mission.ship:
			self.ships[mission.ship] = self.shipStates.idle
		if isinstance(mission, FoundSettlement):
			settlement_manager = SettlementManager(self, mission.land_manager)
			self.settlement_managers.append(settlement_manager)
			self._settlement_manager_by_settlement_id[settlement_manager.settlement.worldid] = settlement_manager
			self.add_building(settlement_manager.settlement.branch_office)
			if settlement_manager.feeder_island:
				self._need_feeder_island = False
		elif isinstance(mission, PrepareFoundationShip):
			self._found_settlements()

	def report_failure(self, mission, msg):
		self.missions.remove(mission)
		if mission.ship:
			self.ships[mission.ship] = self.shipStates.idle
		if isinstance(mission, FoundSettlement):
			del self.islands[mission.land_manager.island.worldid]

	def save(self, db):
		super(AIPlayer, self).save(db)

		# save the player
		db("UPDATE player SET client_id = 'AIPlayer' WHERE rowid = ?", self.worldid)
		current_callback = Callback(self.tick)
		calls = Scheduler().get_classinst_calls(self, current_callback)
		assert len(calls) == 1, "got %s calls for saving %s: %s" % (len(calls), current_callback, calls)
		remaining_ticks = max(calls.values()[0], 1)
		db("INSERT INTO ai_player(rowid, need_more_ships, need_feeder_island, remaining_ticks) VALUES(?, ?, ?, ?)", \
			self.worldid, self.need_more_ships, self._need_feeder_island, remaining_ticks)

		# save the ships
		for ship, state in self.ships.iteritems():
			db("INSERT INTO ai_ship(rowid, owner, state) VALUES(?, ?, ?)", ship.worldid, self.worldid, state.index)

		# save the land managers
		for land_manager in self.islands.itervalues():
			land_manager.save(db)

		# save the settlement managers
		for settlement_manager in self.settlement_managers:
			settlement_manager.save(db)

		# save the missions
		for mission in self.missions:
			mission.save(db)

	def _load(self, db, worldid):
		super(AIPlayer, self)._load(db, worldid)
		self.__init()

		self.need_more_ships, self._need_feeder_island, remaining_ticks = \
			db("SELECT need_more_ships, need_feeder_island, remaining_ticks FROM ai_player WHERE rowid = ?", worldid)[0]
		Scheduler().add_new_object(Callback(self.tick), self, run_in = remaining_ticks)

	def finish_loading(self, db):
		""" This is called separately because most objects are loaded after the player. """

		# load the ships
		for ship_id, state_id in db("SELECT rowid, state FROM ai_ship WHERE owner = ?", self.worldid):
			ship = WorldObject.get_object_by_id(ship_id)
			self.ships[ship] = self.shipStates[state_id]

		# load the land managers
		for (worldid,) in db("SELECT rowid FROM ai_land_manager WHERE owner = ?", self.worldid):
			land_manager = LandManager.load(db, self, worldid)
			self.islands[land_manager.island.worldid] = land_manager

		# load the settlement managers and settlement foundation missions
		for land_manager in self.islands.itervalues():
			db_result = db("SELECT rowid FROM ai_settlement_manager WHERE land_manager = ?", land_manager.worldid)
			if db_result:
				settlement_manager = SettlementManager.load(db, self, db_result[0][0])
				self.settlement_managers.append(settlement_manager)
				self._settlement_manager_by_settlement_id[settlement_manager.settlement.worldid] = settlement_manager

				# load the foundation ship preparing missions
				db_result = db("SELECT rowid FROM ai_mission_prepare_foundation_ship WHERE settlement_manager = ?", \
					settlement_manager.worldid)
				for (mission_id,) in db_result:
					self.missions.add(PrepareFoundationShip.load(db, mission_id, self.report_success, self.report_failure))
			else:
				mission_id = db("SELECT rowid FROM ai_mission_found_settlement WHERE land_manager = ?", land_manager.worldid)[0][0]
				self.missions.add(FoundSettlement.load(db, mission_id, self.report_success, self.report_failure))

		# load the domestic trade missions
		for settlement_manager in self.settlement_managers:
			db_result = db("SELECT rowid FROM ai_mission_domestic_trade WHERE source_settlement_manager = ?", settlement_manager.worldid)
			for (mission_id,) in db_result:
				self.missions.add(DomesticTrade.load(db, mission_id, self.report_success, self.report_failure))

		# TODO: load the international trade missions

	def found_settlement(self, island, ship, feeder_island):
		self.ships[ship] = self.shipStates.on_a_mission
		land_manager = LandManager(island, self, feeder_island)
		land_manager.display()
		self.islands[island.worldid] = land_manager

		found_settlement = FoundSettlement.create(ship, land_manager, self.report_success, self.report_failure)
		self.missions.add(found_settlement)
		found_settlement.start()

	def _have_settlement_starting_resources(self, ship, settlement, min_money, min_resources):
		if self.complete_inventory.money < min_money:
			return False

		for res, amount in ship.inventory:
			if res in min_resources and min_resources[res] > 0:
				min_resources[res] = max(0, min_resources[res] - amount)

		if settlement:
			for res, amount in settlement.inventory:
				if res in min_resources and min_resources[res] > 0:
					min_resources[res] = max(0, min_resources[res] - amount)

		for missing in min_resources.itervalues():
			if missing > 0:
				return False
		return True

	def have_starting_resources(self, ship, settlement):
		return self._have_settlement_starting_resources(ship, settlement, 8000, {RES.BOARDS_ID: 17, RES.FOOD_ID: 10, RES.TOOLS_ID: 5})

	def have_feeder_island_starting_resources(self, ship, settlement):
		return self._have_settlement_starting_resources(ship, settlement, 4000, {RES.BOARDS_ID: 20, RES.TOOLS_ID: 10})

	def prepare_foundation_ship(self, settlement_manager, ship, feeder_island):
		self.ships[ship] = self.shipStates.on_a_mission
		mission = PrepareFoundationShip(settlement_manager, ship, feeder_island, self.report_success, self.report_failure)
		self.missions.add(mission)
		mission.start()

	def tick(self):
		Scheduler().add_new_object(Callback(self.tick), self, run_in = 37)
		self._found_settlements()
		self.manage_international_trade()
		self.handle_enemy_expansions()

	def _found_settlements(self):
		ship = None
		for possible_ship, state in self.ships.iteritems():
			if state is self.shipStates.idle:
				ship = possible_ship
				break
		if not ship:
			#self.log.info('%s.tick: no available ships', self)
			return

		island = None
		sequence = [500, 300, 150]
		for min_size in sequence:
			island = self.choose_island(min_size)
			if island is not None:
				break
		if island is None:
			#self.log.info('%s.tick: no good enough islands', self)
			return

		if self._need_feeder_island:
			if self.have_feeder_island_starting_resources(ship, None):
				self.log.info('%s.tick: send %s on a mission to found a feeder settlement', self, ship)
				self.found_settlement(island, ship, True)
			else:
				for settlement_manager in self.settlement_managers:
					if self.have_feeder_island_starting_resources(ship, settlement_manager.land_manager.settlement):
						self.log.info('%s.tick: send ship %s on a mission to get resources for a new feeder settlement', self, ship)
						self.prepare_foundation_ship(settlement_manager, ship, True)
						return
		elif self.want_another_village():
			if self.have_starting_resources(ship, None):
				self.log.info('%s.tick: send ship %s on a mission to found a settlement', self, ship)
				self.found_settlement(island, ship, False)
			else:
				for settlement_manager in self.settlement_managers:
					if not settlement_manager.can_provide_resources():
						continue
					if self.have_starting_resources(ship, settlement_manager.land_manager.settlement):
						self.log.info('%s.tick: send ship %s on a mission to get resources for a new settlement', self, ship)
						self.prepare_foundation_ship(settlement_manager, ship, False)
						return

	def want_another_village(self):
		""" Avoid having more than one developing island with a village at a time """
		for settlement_manager in self.settlement_managers:
			if not settlement_manager.feeder_island and not settlement_manager.can_provide_resources():
				return False
		return True

	@classmethod
	def need_feeder_island(cls, settlement_manager):
		return settlement_manager.production_builder.count_available_squares(3, 30)[1] < 30

	def have_feeder_island(self):
		for settlement_manager in self.settlement_managers:
			if not self.need_feeder_island(settlement_manager):
				return True
		return False

	def can_found_feeder_island(self):
		islands = self.get_available_islands(400)
		return len(islands) > 0

	def found_feeder_island(self):
		if self.can_found_feeder_island():
			self._need_feeder_island = True

	def request_ship(self):
		self.log.info('%s received request for more ships', self)
		self.need_more_ships = True

	def add_building(self, building):
		# if the id is not present then this is a new settlement that has to be handled separately
		if building.settlement.worldid in self._settlement_manager_by_settlement_id:
			self._settlement_manager_by_settlement_id[building.settlement.worldid].add_building(building)

	def remove_building(self, building):
		self._settlement_manager_by_settlement_id[building.settlement.worldid].remove_building(building)

	def count_buildings(self, building_id):
		return sum(settlement_manager.count_buildings(building_id) for settlement_manager in self.settlement_managers)

	def notify_unit_path_blocked(self, unit):
		self.log.warning("%s ship blocked (%s)", self, unit)

	def _international_trade_mission_exists(self, settlement, settlement_manager):
		for mission in self.missions:
			if not isinstance(mission, InternationalTrade):
				continue
			if mission.settlement is settlement and mission.settlement_manager is settlement_manager:
				return True
		return False

	def manage_international_trade(self):
		ship_capacity = 120 # TODO: handle different ship capacities
		little_money = 3000 # cutoff to decide that we really need to get more money

		ship = None
		for possible_ship, state in self.ships.iteritems():
			if state is self.shipStates.idle:
				ship = possible_ship
				break
		if not ship:
			#self.log.info('%s international trade: no available ships', self)
			return

		options = defaultdict(lambda: [])
		for settlement in self.world.settlements:
			if settlement.owner is self:
				continue
			for settlement_manager in self.settlement_managers:
				if self._international_trade_mission_exists(settlement, settlement_manager):
					continue # allow only one international trade route between a pair of settlements
				my_inventory = settlement_manager.settlement.inventory
				resource_manager = settlement_manager.resource_manager

				for resource_id, limit in settlement.buy_list.iteritems():
					if resource_id not in resource_manager.resource_requirements:
						continue # not a well-known resource: ignore it
					if limit <= settlement.inventory[resource_id]:
						continue # they aren't actually buying the resource
					if my_inventory[resource_id] <= resource_manager.resource_requirements[resource_id]:
						continue # my settlement is unable to sell the resource
					price = int(self.session.db.get_res_value(resource_id) * TRADER.PRICE_MODIFIER_SELL)
					tradable_amount = min(my_inventory[resource_id] - resource_manager.resource_requirements[resource_id], \
						limit - settlement.inventory[resource_id], ship_capacity, settlement.owner.inventory[RES.GOLD_ID] // price)
					options[(settlement, settlement_manager)].append((tradable_amount * price, tradable_amount, resource_id, True))

				for resource_id, limit in settlement.sell_list.iteritems():
					if resource_id not in resource_manager.resource_requirements:
						continue # not a well-known resource: ignore it
					if limit >= settlement.inventory[resource_id]:
						continue # they aren't actually selling the resource
					if my_inventory[resource_id] >= resource_manager.resource_requirements[resource_id]:
						continue # my settlement doesn't want to buy the resource
					price = int(self.session.db.get_res_value(resource_id) * TRADER.PRICE_MODIFIER_BUY)
					tradable_amount = min(resource_manager.resource_requirements[resource_id] - my_inventory[resource_id], \
						settlement.inventory[resource_id] - limit, ship_capacity, self.inventory[RES.GOLD_ID] // price)
					options[(settlement, settlement_manager)].append((tradable_amount * price, tradable_amount, resource_id, False))
		if not options:
			#self.log.info('%s international trade: no interesting options', self)
			return

		final_options = []
		for (settlement, settlement_manager), option in sorted(options.iteritems()):
			best_buy = None # largest amount of resources
			best_sale = None # most expensive sale
			for total_price, tradable_amount, resource_id, selling in option:
				if selling:
					if best_sale is None or best_sale[0] < total_price:
						best_sale = (total_price, tradable_amount, resource_id)
				else:
					if best_buy is None or best_buy[1] < tradable_amount:
						best_buy = (total_price, tradable_amount, resource_id)
			total_value = (best_sale[0] if best_sale else 0) + (best_buy[1] if best_buy else 0) * (30 if self.inventory[RES.GOLD_ID] > little_money else 10)
			# TODO: make settlement and settlement_manager properly sortable
			final_options.append((total_value, best_buy[2] if best_buy else None, best_sale[2] if best_sale else None, settlement, settlement_manager))

		bought_resource, sold_resource, settlement, settlement_manager = max(final_options)[1:]
		mission = InternationalTrade(settlement_manager, settlement, ship, bought_resource, sold_resource, self.report_success, self.report_failure)
		self.ships[ship] = self.shipStates.on_a_mission
		self.missions.add(mission)
		mission.start()

	def on_settlement_expansion(self, settlement, coords):
		""" stores the ownership change in a list for later processing """
		if settlement.owner is not self:
			self.settlement_expansions.append((coords, settlement))

	def handle_enemy_expansions(self):
		if not self.settlement_expansions:
			return # no changes in land ownership

		change_lists = defaultdict(lambda: [])
		for coords, settlement in self.settlement_expansions:
			if settlement.island.worldid not in self.islands:
				continue # we don't have a settlement there and have no current plans to create one
			change_lists[settlement.island.worldid].append(coords)
		self.settlement_expansions = []
		if not change_lists:
			return # no changes in land ownership on islands we care about

		for island_id, changed_coords in change_lists.iteritems():
			affects_us = False
			land_manager = self.islands[island_id]
			for coords in changed_coords:
				if coords in land_manager.production or coords in land_manager.village:
					affects_us = True
					break
			if not affects_us:
				continue # we weren't using that land anyway

			settlement_manager = None
			for potential_settlement_manager in self.settlement_managers:
				if potential_settlement_manager.settlement.island.worldid == island_id:
					settlement_manager = potential_settlement_manager
					break

			if settlement_manager is None:
				self.handle_enemy_settling_on_our_chosen_island(island_id)
				# we are on the way to found a settlement on that island
			else:
				# we already have a settlement there
				settlement_manager.handle_lost_area(changed_coords)

	def handle_enemy_settling_on_our_chosen_island(self, island_id):
		mission = None
		for a_mission in self.missions:
			if isinstance(a_mission, FoundSettlement) and a_mission.land_manager.island.worldid == island_id:
				mission = a_mission
				break
		assert mission
		mission.cancel()
		self._found_settlements()

	@classmethod
	def load_abstract_buildings(cls, db):
		AbstractBuilding.load_all(db)

	def __str__(self):
		return 'AI(%s/%d)' % (self.name if hasattr(self, 'name') else 'unknown', self.worldid)

decorators.bind_all(AIPlayer)
