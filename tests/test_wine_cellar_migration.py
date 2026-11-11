"""Exercise the kiosk battery merge without starting pygame or hardware."""
import ast
import time
import unittest
from pathlib import Path

class WineCellarMigrationTests(unittest.TestCase):
    def test_retired_smartthings_reading_is_replaced_by_yolink_battery(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'startrek.py').read_text())
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='battery_inventory')
        namespace={'time':time}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'battery_inventory','exec'),namespace)
        old={'house':{'batteries':{'old':{'name':'Old Wine Cellar Temp','level_pct':0},'legacy':{'name':'wine cellar temp','level_pct':0},'fridge':{'name':'Wine Fridge','level_pct':10}}}}
        new={'temperature_sensors':[{'name':'Wine Cellar','battery':4,'reported_at':time.time(),'online':True}]}
        result=namespace['battery_inventory'](old,new)
        self.assertEqual({row['name'] for row in result},{'Wine Fridge','Wine Cellar'})
        cellar=next(row for row in result if row['name']=='Wine Cellar')
        self.assertEqual(cellar['source'],'YOLINK')
        self.assertEqual(cellar['value_text'],'4/4')
        self.assertEqual(cellar['condition'],'GOOD')
