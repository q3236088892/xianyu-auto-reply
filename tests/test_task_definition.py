import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class TaskDefinitionTests(unittest.TestCase):
 def test_install_script_targets_daily_0330_update(self):
  path=ROOT/'scripts'/'install-update-task.ps1'; self.assertTrue(path.is_file())
  text=path.read_text(encoding='utf-8')
  self.assertIn('03:30',text); self.assertIn('UPDATE.cmd',text); self.assertIn('XianyuAutoReply-Zhinianboke-Portable-Update',text)
 def test_remove_script_only_removes_task(self):
  path=ROOT/'scripts'/'remove-update-task.ps1'; self.assertTrue(path.is_file())
  text=path.read_text(encoding='utf-8'); self.assertIn('Unregister-ScheduledTask',text); self.assertNotIn('Remove-Item',text)
if __name__=='__main__': unittest.main()
