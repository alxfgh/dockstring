import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, List, Union

from rdkit.Chem import AllChem as Chem

from .errors import DockstringError, VinaError
from .utils import (smiles_to_mol, embed_mol, refine_mol_with_ff, convert_pdbqt_to_pdb, read_mol_from_pdb,
                    parse_scores_from_output, parse_search_box_conf, PathType, get_targets_dir, get_vina_path,
                    get_resources_dir, check_mol, canonicalize_smiles, verify_docked_ligand, check_vina_output,
                    assign_stereochemistry, assign_bond_orders, sanitize_mol, protonate_mol, write_mol_to_mol_file,
                    convert_mol_file_to_pdbqt, check_charges)


def load_target(name: str, *args, **kwargs):
    return Target(name, *args, **kwargs)


def list_all_target_names() -> List[str]:
    targets_dir = get_targets_dir()
    file_names = [f for f in os.listdir(targets_dir) if os.path.isfile(os.path.join(targets_dir, f))]

    target_re = re.compile(r'^(?P<name>\w+)_target\.pdb$')
    names = []
    for file_name in file_names:
        match = target_re.match(file_name)
        if match:
            names.append(match.group('name'))

    return names


class Target:
    def __init__(self, name, working_dir: Optional[PathType] = None):
        self.name = name

        # Directory where the ligand and output files will be saved
        self._custom_working_dir = working_dir
        self._tmp_dir_handle: Optional[tempfile.TemporaryDirectory] = None

        # Ensure input files exist
        if not all(p.exists() for p in [self.pdb_path, self.pdbqt_path, self.conf_path]):
            raise DockstringError(f"'{self.name}' is not a supported target")

    def __repr__(self):
        return f"Target(name='{self.name}', working_dir='{self.working_dir}')"

    @property
    def pdb_path(self) -> Path:
        return get_targets_dir() / (self.name + '_target.pdb')

    @property
    def pdbqt_path(self) -> Path:
        return get_targets_dir() / (self.name + '_target.pdbqt')

    @property
    def conf_path(self) -> Path:
        return get_targets_dir() / (self.name + '_conf.txt')

    @property
    def working_dir(self) -> Path:
        if self._custom_working_dir:
            return Path(self._custom_working_dir).resolve()

        # If no custom working dir is set and the tmp working dir handle is not initialized, initialize it
        if not self._tmp_dir_handle:
            self._tmp_dir_handle = tempfile.TemporaryDirectory()

        return Path(self._tmp_dir_handle.name).resolve()

    def _dock_pdbqt(self,
                    ligand_pdbqt,
                    vina_logfile,
                    vina_outfile,
                    seed,
                    num_cpus: Optional[int] = None,
                    verbose=False):
        # yapf: disable
        cmd_list = [
            get_vina_path(),
            '--receptor', self.pdbqt_path,
            '--config', self.conf_path,
            '--ligand', ligand_pdbqt,
            '--log', vina_logfile,
            '--out', vina_outfile,
            '--seed', str(seed),
        ]
        # yapf: enable
        if num_cpus is not None:
            cmd_list += ['--cpu', str(num_cpus)]

        cmd_return = subprocess.run(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = cmd_return.stdout.decode('utf-8')

        if verbose:
            logging.info(output)

        # If failure, raise DockingError
        if cmd_return.returncode != 0:
            raise VinaError('Docking with Vina failed')

    def dock(self, smiles: str, num_cpus: Optional[int] = None, seed=974528263, verbose=False):
        """
        Given a molecule, this method will return a docking score against the current target.
        - smiles: SMILES string
        - num_cpus: number of cpus that AutoDock Vina should use for the docking. By default,
          it will try to find all the cpus on the system, and failing that, it will use 1.
        - seed: integer random seed for reproducibility
        """

        # Auxiliary files
        ligand_mol_file = self.working_dir / 'ligand.mol'
        ligand_pdbqt = self.working_dir / 'ligand.pdbqt'
        vina_logfile = self.working_dir / 'vina.log'
        vina_outfile = self.working_dir / 'vina.out'
        docked_ligand_pdb = self.working_dir / 'docked_ligand.pdb'

        # Make sure user input is standardized
        canonical_smiles = canonicalize_smiles(smiles)

        # Read and check input
        mol = smiles_to_mol(canonical_smiles, verbose=verbose)
        mol = sanitize_mol(mol, verbose=verbose)
        check_mol(mol)
        check_charges(mol)

        # Protonate ligand
        protonated_mol = protonate_mol(mol)
        check_mol(protonated_mol)

        # Embed ligand
        embedded_mol = embed_mol(protonated_mol, seed=seed)
        refined_mol = refine_mol_with_ff(embedded_mol)
        assign_stereochemistry(refined_mol)

        # Dock
        write_mol_to_mol_file(refined_mol, ligand_mol_file)
        convert_mol_file_to_pdbqt(ligand_mol_file, ligand_pdbqt, verbose=verbose)
        self._dock_pdbqt(ligand_pdbqt, vina_logfile, vina_outfile, seed=seed, num_cpus=num_cpus, verbose=verbose)

        # Process docking output
        check_vina_output(vina_outfile)
        convert_pdbqt_to_pdb(pdbqt_file=vina_outfile, pdb_file=docked_ligand_pdb, disable_bonding=True, verbose=verbose)
        raw_ligand = read_mol_from_pdb(docked_ligand_pdb)

        # Assign bond orders and stereochemistry
        refined_mol_no_hs = Chem.RemoveHs(refined_mol)  # remove Hs as they are not present in the PDBQT file
        ligand = assign_bond_orders(subject=raw_ligand, ref=refined_mol_no_hs)
        assign_stereochemistry(ligand)

        # Verify docked ligand
        verify_docked_ligand(ref=refined_mol_no_hs, ligand=ligand)

        # Parse scores
        scores = parse_scores_from_output(docked_ligand_pdb)
        assert len(scores) == ligand.GetNumConformers()

        return scores[0], {
            'ligand': ligand,
            'scores': scores,
        }

    def view(self, mols: List[Chem.Mol] = None, search_box=True):
        """
        Start pymol and view the receptor and the search box.
        """
        commands: List[Union[str, PathType]] = ['pymol', self.pdb_path]

        if search_box:
            pymol_script = get_resources_dir() / 'view_search_box.py'
            conf = parse_search_box_conf(self.conf_path)
            # yapf: disable
            commands += [
                pymol_script,
                '-d', 'view_search_box center_x={center_x}, center_y={center_y}, center_z={center_z}, '
                      'size_x={size_x}, size_y={size_y}, size_z={size_z}'.format(**conf)
            ]
            # yapf: enable

        if mols:
            tmp_dir_handle = tempfile.TemporaryDirectory()
            tmp_dir = Path(tmp_dir_handle.name).resolve()

            for index, mol in enumerate(mols):
                mol_file = tmp_dir / f'ligand_{index}.mol'
                write_mol_to_mol_file(mol, mol_file)
                commands += [str(mol_file)]

        return subprocess.run(commands)
