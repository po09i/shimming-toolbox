# -*- coding: utf-8 -*-
"""
This file includes CLIs for shimming by fitting fieldmaps for static and realtime shimming. It groups them along with
the gradient method in a st_shim CLI with the argument being:
- fieldmap_static
- fieldmap_realtime
- gradient_realtime
"""
import click
import copy
import json
import math
import nibabel as nib
import numpy as np
import logging
import os
from matplotlib.figure import Figure

from shimmingtoolbox import __dir_config_scanner_constraints__
from shimmingtoolbox.cli.realtime_shim import realtime_shim_cli
from shimmingtoolbox.coils.coil import Coil, ScannerCoil, convert_to_mp
from shimmingtoolbox.pmu import PmuResp
from shimmingtoolbox.shim.sequencer import shim_sequencer, shim_realtime_pmu_sequencer, new_bounds_from_currents
from shimmingtoolbox.shim.sequencer import extend_slice, define_slices
from shimmingtoolbox.utils import create_output_dir, set_all_loggers
from shimmingtoolbox.shim.shim_utils import phys_to_gradient_cs

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@click.group(context_settings=CONTEXT_SETTINGS,
             help="Shim according to the specified algorithm as an argument e.g. st_b0shim xxxxx")
def b0shim_cli():
    pass


@click.command(context_settings=CONTEXT_SETTINGS)
@click.option('--coil', 'coils', nargs=2, multiple=True, type=(click.Path(exists=True), click.Path(exists=True)),
              help="Pair of filenames containing the coil profiles followed by the filename to the constraints "
                   "e.g. --coil a.nii cons.json. If you have more than one coil, use this option more than once. "
                   "The coil profiles and the fieldmaps (--fmap) must have matching units (if fmap is in Hz, the coil "
                   "profiles must be in Hz/unit_shim). If using the scanner's gradient/shim coils, the coil profiles "
                   "must be in Hz/unit_shim and fieldmaps must be in Hz. If you want to shim using the scanner's "
                   "gradient/shim coils, use the `--scanner-coil-order` option. For an example of a constraint file, "
                   f"see: {__dir_config_scanner_constraints__}")
@click.option('--fmap', 'fname_fmap', required=True, type=click.Path(exists=True),
              help="Static B0 fieldmap.")
@click.option('--anat', 'fname_anat', type=click.Path(exists=True), required=True,
              help="Anatomical image to apply the correction onto.")
@click.option('--mask', 'fname_mask_anat', type=click.Path(exists=True), required=False,
              help="Mask defining the spatial region to shim."
                   "The coordinate system should be the same as ``anat``'s coordinate system.")
@click.option('--scanner-coil-order', type=click.Choice(['-1', '0', '1']), default='-1', show_default=True,
              help="Maximum order of the shim system. Note that specifying 1 will return "
                   "orders 0 and 1. The 0th order is the f0 frequency.")
@click.option('--scanner-coil-constraints', 'fname_sph_constr', type=click.Path(exists=True),
              default=__dir_config_scanner_constraints__, show_default=True,
              help="Constraints for the scanner coil.")
@click.option('--slices', type=click.Choice(['interleaved', 'sequential', 'volume']), required=False,
              default='sequential', show_default=True, help="Defines the slice ordering.")
@click.option('--slice-factor', 'slice_factor', type=click.INT, required=False, default=1, show_default=True,
              help="Number of slices per shimmed group. For example, if the value is '3', then with the 'sequential' "
                   "mode, shimming will be performed independently on the following groups: {0,1,2}, {3,4,5}, etc. "
                   "With the mode 'interleaved', it will be: {0,2,4}, {1,3,5}, etc.")
@click.option('--optimizer-method', 'method', type=click.Choice(['least_squares', 'pseudo_inverse']), required=False,
              default='least_squares', show_default=True,
              help="Method used by the optimizer. LS will respect the constraints, PS will not respect the constraints")
@click.option('--mask-dilation-kernel-size', 'dilation_kernel_size', type=click.INT, required=False, default='3',
              show_default=True,
              help="Number of voxels to consider outside of the masked area. For example, when doing dynamic shimming "
                   "with a linear gradient, the coefficient corresponding to the gradient orthogonal to a single "
                   "slice cannot be estimated: there must be at least 2 (ideally 3) points to properly estimate the "
                   "linear term. When using 2nd order or more, more dilation is necessary.")
@click.option('-o', '--output', 'path_output', type=click.Path(), default=os.path.abspath(os.curdir),
              show_default=True, help="Directory to output coil text file(s).")
@click.option('--output-file-format-coil', 'o_format_coil',
              type=click.Choice(['slicewise-ch', 'slicewise-coil', 'chronological-ch', 'chronological-coil']),
              default='slicewise-coil',
              show_default=True, help="Syntax used to describe the sequence of shim events for custom coils. "
                                      "Use 'slicewise' to output in row 1, 2, 3, etc. the shim coefficients for slice "
                                      "1, 2, 3, etc. Use 'chronological' to output in row 1, 2, 3, etc. the shim value "
                                      "for trigger 1, 2, 3, etc. The trigger is an event sent by the scanner and "
                                      "captured by the controller of the shim amplifier. Use 'ch' to output one "
                                      "file per coil channel (coil1_ch1.txt, coil1_ch2.txt, etc.). Use 'coil' to "
                                      "output one file per coil system (coil1.txt, coil2.txt). In the latter case, "
                                      "all coil channels are encoded across multiple columns in the text file.")
@click.option('--output-file-format-scanner', 'o_format_sph',
              type=click.Choice(['slicewise-ch', 'slicewise-coil', 'chronological-ch', 'chronological-coil']),
              default='slicewise-coil',
              show_default=True, help="Syntax used to describe the sequence of shim events for scanner coils. "
                                      "Use 'slicewise' to output in row 1, 2, 3, etc. the shim coefficients for slice "
                                      "1, 2, 3, etc. Use 'chronological' to output in row 1, 2, 3, etc. the shim value "
                                      "for trigger 1, 2, 3, etc. The trigger is an event sent by the scanner and "
                                      "captured by the controller of the shim amplifier. Use 'ch' to output one "
                                      "file per coil channel (coil1_ch1.txt, coil1_ch2.txt, etc.). Use 'coil' to "
                                      "output one file per coil system (coil1.txt, coil2.txt). In the latter case, "
                                      "all coil channels are encoded across multiple columns in the text file.")
@click.option('--output-value-format', 'output_value_format', type=click.Choice(['delta', 'absolute']), default='delta',
              show_default=True,
              help="Coefficient values for the scanner coil. Delta: Outputs the change of shim coefficients. The "
                   "scanner coil coefficients will be in the Gradient coordinate system. Absolute: Outputs the "
                   "absolute coefficient by taking into account the current shim settings. This is effectively "
                   "initial + shim. Scanner coil coefficients will be in the Shim coordinate system.")
@click.option('-v', '--verbose', type=click.Choice(['info', 'debug']), default='info', help="Be more verbose")
def static_cli(fname_fmap, fname_anat, fname_mask_anat, method, slices, slice_factor, coils,
               dilation_kernel_size, scanner_coil_order, fname_sph_constr, path_output, o_format_coil, o_format_sph,
               output_value_format, verbose):
    """ Static shim by fitting a fieldmap. Use the option --optimizer-method to change the shimming algorithm used to
    optimize. Use the options --slices and --slice-factor to change the shimming order/size of the slices.

    Example of use: st_b0shim static --coil coil1.nii coil1_config.json --coil coil2.nii coil2_config.json
    --fmap fmap.nii --anat anat.nii --mask mask.nii --optimizer-method least_squares
    """
    # Set logger level
    set_all_loggers(verbose)

    # Prepare the output
    create_output_dir(path_output)

    # Input scanner_coil_order can be a string
    scanner_coil_order = int(scanner_coil_order)

    # Load the fieldmap, expand the dimensions of the fieldmap if one of the dimensions is 2 or less. This is done since
    # we are fitting a fieldmap to coil profiles, having essentially a 2d matrix as a fieldmap can lead to errors in the
    # through plane direction.
    fmap_required_dims = 3
    nii_fmap = _load_fmap(fname_fmap, fmap_required_dims, dilation_kernel_size, path_output)

    # Load the anat
    nii_anat = nib.load(fname_anat)
    dim_info = nii_anat.header.get_dim_info()
    if dim_info[2] != 2:
        # Slice must be the 3rd dimension of the file
        # TODO: Reorient nifti so that the slice is the 3rd dim
        raise RuntimeError("Slice encode direction must be the 3rd dimension of the NIfTI file.")

    # Load mask
    if fname_mask_anat is not None:
        nii_mask_anat = nib.load(fname_mask_anat)
    else:
        # If no mask is provided, shim the whole anat volume
        nii_mask_anat = nib.Nifti1Image(np.ones_like(nii_anat.get_fdata()), nii_anat.affine, header=nii_anat.header)

    if logger.level <= getattr(logging, 'DEBUG'):
        # Save inputs
        list_fname = [fname_fmap, fname_anat, fname_mask_anat]
        _save_nii_to_new_dir(list_fname, path_output)

    # Open json of the fmap
    fname_json = fname_fmap.split('.nii')[0] + '.json'
    # Read from json file
    if os.path.isfile(fname_json):
        json_fm_data = json.load(open(fname_json))
    else:
        raise OSError("Missing fieldmap json file")

    # Get the initial coefficients from the json file (Tx + 1st + 2nd order shim)
    json_coefs = _get_current_shim_settings(json_fm_data)
    converted_coefs = convert_to_mp(json_coefs[1:], json_fm_data['ManufacturersModelName'])
    initial_coefs = [json_coefs[0]] + converted_coefs

    # Load the coils
    list_coils = _load_coils(coils, scanner_coil_order, fname_sph_constr, nii_fmap, initial_coefs)

    # Get the shim slice ordering
    n_slices = nii_anat.shape[2]
    list_slices = define_slices(n_slices, slice_factor, slices)
    logger.info(f"The slices to shim are:\n{list_slices}")

    # Get shimming coefficients
    coefs = shim_sequencer(nii_fmap, nii_anat, nii_mask_anat, list_slices, list_coils,
                           method=method,
                           mask_dilation_kernel='sphere',
                           mask_dilation_kernel_size=dilation_kernel_size,
                           path_output=path_output)

    # Output
    list_fname_output = []
    end_channel = 0
    for i_coil, coil in enumerate(list_coils):

        # Figure out the start and end channels for a coil to be able to select it from the coefs
        n_channels = coil.dim[3]
        start_channel = end_channel
        end_channel = start_channel + n_channels

        # Select the coefficients for a coil
        coefs_coil = copy.deepcopy(coefs[:, start_channel:end_channel])

        # If it's a scanner
        if type(coil) == ScannerCoil:

            if output_value_format == 'delta' and scanner_coil_order >= 1:
                logger.debug("Converting scanner coil from Physical CS (RAS) to Gradient CS")
                # TODO: Fix for 2nd order (must validate 2nd order siemens basis)
                # Convert coef of 1st order sph harmonics to Gradient coord system
                coefs_freq, coefs_phase, coefs_slice = phys_to_gradient_cs(coefs_coil[:, 1],
                                                                           coefs_coil[:, 2],
                                                                           coefs_coil[:, 3], fname_anat)

                coefs_coil[:, 1] = coefs_freq
                coefs_coil[:, 2] = coefs_phase
                coefs_coil[:, 3] = coefs_slice

                # # Plot a figure of the coefficients, order 0 is in Hz, order 1 in mt/m, order 2 in mt/m^2
                # units = "Gradient CS [mT/m]"
                # _plot_coefs(coil, list_slices, coefs[:, start_channel:end_channel], path_output, i_coil, units=units)

            else:  # output_value_format == 'absolute'
                # Load anat json
                fname_anat_json = fname_anat.rsplit('.nii', 1)[0] + '.json'
                with open(fname_anat_json) as json_file:
                    json_anat_data = json.load(json_file)

                if json_anat_data['Manufacturer'] == 'Siemens':
                    # Change from RAS to LAI (ShimCS)
                    # x
                    coefs_coil[:, 1] = -coefs_coil[:, 1]
                    # z
                    coefs_coil[:, 3] = -coefs_coil[:, 3]

                    # Bounds also change from RAS to LAI
                    bounds_shim_cs = np.array(coil.coef_channel_minmax)
                    bounds_shim_cs[1] = -bounds_shim_cs[1]
                    bounds_shim_cs[3] = -bounds_shim_cs[3]
                else:
                    raise NotImplementedError(f"Manufacturer: {json_anat_data['Manufacturer']} not yet implemented for"
                                              f"absolute format")

                # # Plot a figure of the coefficients (Delta), order 0 is in Hz, order 1 in mt/m, order 2 in mt/m^2
                # units = "ShimCS [mT/m]"
                # _plot_coefs(coil, list_slices, coefs_coil, path_output, i_coil, units=units, bounds=bounds_shim_cs)

                for i_channel in range(n_channels):
                    # abs_coef = delta + initial
                    coefs_coil[:, i_channel] = coefs_coil[:, i_channel] + initial_coefs[i_channel]

            list_fname_output += _save_to_text_file_static(coil, coefs_coil, list_slices, path_output, o_format_sph,
                                                           coil_number=i_coil)
        else:
            list_fname_output += _save_to_text_file_static(coil, coefs_coil, list_slices, path_output, o_format_coil,
                                                           coil_number=i_coil)
            # Plot a figure of the coefficients
            _plot_coefs(coil, list_slices, coefs_coil, path_output, i_coil, bounds=coil.coef_channel_minmax)

    logger.info(f"Coil txt file(s) are here:\n{os.linesep.join(list_fname_output)}")


def _save_to_text_file_static(coil, coefs, list_slices, path_output, o_format, coil_number):
    """o_format can either be 'slicewise-ch', 'slicewise-coil', 'chronological-ch', 'chronological-coil'"""

    n_channels = coil.dim[3]
    list_fname_output = []
    if o_format[-5:] == '-coil':

        fname_output = os.path.join(path_output, f"coefs_coil{coil_number}_{coil.name}.txt")
        with open(fname_output, 'w', encoding='utf-8') as f:
            # (len(slices) x n_channels)

            if o_format == 'chronological-coil':
                # Output per shim (chronological), output all channels for a particular shim, then repeat
                for i_shim in range(len(list_slices)):
                    for i_channel in range(n_channels):
                        f.write(f"{coefs[i_shim, i_channel]:.6f}")
                        if i_channel != n_channels:
                            f.write(", ")
                    f.write("\n")

            elif o_format == 'slicewise-coil':
                # Output per slice, output all channels for a particular slice, then repeat
                # Assumes all slices are in list_slices once which is the case for sequential, interleaved and
                # volume
                n_slices = np.sum([len(a_shim) for a_shim in list_slices])
                for i_slice in range(n_slices):
                    i_shim = [list_slices.index(a_shim) for a_shim in list_slices if i_slice in a_shim][0]
                    for i_channel in range(n_channels):
                        f.write(f"{coefs[i_shim, i_channel]:.6f}")
                        if i_channel != n_channels:
                            f.write(", ")
                    f.write("\n")

        list_fname_output.append(os.path.abspath(fname_output))

    else:
        # o_format[-3:] == '-ch':
        # Write a file for each channel
        for i_channel in range(n_channels):
            fname_output = os.path.abspath(os.path.join(path_output,
                                                        f"coefs_coil{coil_number}_ch{i_channel}_{coil.name}.txt"))

            if o_format == 'chronological-ch':
                with open(fname_output, 'w', encoding='utf-8') as f:
                    # Each row will have one coef representing the shim in chronological order
                    for i_shim in range(len(list_slices)):
                        f.write(f"{coefs[i_shim, i_channel]:.6f}\n")

            if o_format == 'slicewise-ch':
                with open(fname_output, 'w', encoding='utf-8') as f:
                    # Each row will have one coef representing the shim in slicewise order
                    n_slices = np.sum([len(a_tuple) for a_tuple in list_slices])
                    for i_slice in range(n_slices):
                        i_shim = [list_slices.index(i) for i in list_slices if i_slice in i][0]
                        f.write(f"{coefs[i_shim, i_channel]:.6f}\n")

            list_fname_output.append(os.path.abspath(fname_output))

    return list_fname_output


@click.command(context_settings=CONTEXT_SETTINGS)
@click.option('--coil', 'coils', nargs=2, multiple=True, type=(click.Path(exists=True), click.Path(exists=True)),
              help="Pair of filenames containing the coil profiles followed by the filename to the constraints "
                   "e.g. --coil a.nii cons.json. If you have more than one coil, use this option more than once. "
                   "The coil profiles and the fieldmaps (--fmap) must have matching units (if fmap is in Hz, the coil "
                   "profiles must be in Hz/unit_shim). If you only want to shim using the scanner's gradient/shim "
                   "coils, use the `--scanner-coil-order` option. For an example of a constraint file, "
                   f"see: {__dir_config_scanner_constraints__}")
@click.option('--fmap', 'fname_fmap', required=True, type=click.Path(exists=True),
              help="Timeseries of B0 fieldmap.")
@click.option('--anat', 'fname_anat', type=click.Path(exists=True), required=True,
              help="Anatomical image to apply the correction onto.")
@click.option('--resp', 'fname_resp', type=click.Path(exists=True), required=True,
              help="Siemens respiratory file containing pressure data.")
@click.option('--mask-static', 'fname_mask_anat_static', type=click.Path(exists=True), required=False,
              help="Mask defining the static spatial region to shim."
                   "The coordinate system should be the same as ``anat``'s coordinate system.")
@click.option('--mask-riro', 'fname_mask_anat_riro', type=click.Path(exists=True), required=False,
              help="Mask defining the time varying (i.e. RIRO, Respiration-Induced Resonance Offset) "
                   "region to shim. The coordinate system should be the same as ``anat``'s coordinate system.")
@click.option('--scanner-coil-order', type=click.Choice(['-1', '0', '1']), default='-1', show_default=True,
              help="Maximum order of the shim system. Note that specifying 1 will return "
                   "orders 0 and 1. The 0th order is the f0 frequency.")
@click.option('--scanner-coil-constraints', 'fname_sph_constr', type=click.Path(exists=True),
              default=__dir_config_scanner_constraints__, show_default=True,
              help="Constraints for the scanner coil.")
@click.option('--slices', type=click.Choice(['interleaved', 'sequential', 'volume']), required=False,
              default='sequential', show_default=True, help="Defines the slice ordering")
@click.option('--slice-factor', 'slice_factor', type=click.INT, required=False, default=1, show_default=True,
              help="Number of slices per shimmed group. For example, if the value is '3', then with the 'sequential' "
                   "mode, shimming will be performed independently on the following groups: {0,1,2}, {3,4,5}, etc. "
                   "With the mode 'interleaved', it will be: {0,2,4}, {1,3,5}, etc.")
@click.option('--optimizer-method', 'method', type=click.Choice(['least_squares', 'pseudo_inverse']), required=False,
              default='least_squares', show_default=True,
              help="Method used by the optimizer. LS will respect the constraints, PS will not respect the constraints")
@click.option('--mask-dilation-kernel-size', 'dilation_kernel_size', type=click.INT, required=False, default='3',
              show_default=True,
              help="Number of voxels to consider outside of the masked area. For example, when doing dynamic shimming "
                   "with a linear gradient, the coefficient corresponding to the gradient orthogonal to a single "
                   "slice cannot be estimated: there must be at least 2 (ideally 3) points to properly estimate the "
                   "linear term. When using 2nd order or more, more dilation is necessary.")
@click.option('-o', '--output', 'path_output', type=click.Path(), default=os.path.abspath(os.curdir),
              show_default=True, help="Directory to output coil text file(s).")
@click.option('--output-file-format', 'o_format', type=click.Choice(['slicewise-ch', 'chronological-ch', 'eva']),
              default='slicewise-ch',
              show_default=True, help="Syntax used to describe the sequence of shim events. "
                                      "Use 'slicewise' to output in row 1, 2, 3, etc. the shim coefficients for slice "
                                      "1, 2, 3, etc. Use 'chronological' to output in row 1, 2, 3, etc. the shim value "
                                      "for trigger 1, 2, 3, etc. The trigger is an event sent by the scanner and "
                                      "captured by the controller of the shim amplifier. There will be one output "
                                      "file per coil channel (coil1_ch1.txt, coil1_ch2.txt, etc.). The static, "
                                      "time-varying and mean pressure are encoded in the columns of each file.")
@click.option('--output-value-format', 'output_value_format', type=click.Choice(['delta', 'absolute']),
              default='delta', show_default=True,
              help="Coefficient values for the scanner coil. Delta: Outputs the change of shim coefficients. The "
                   "scanner coil coefficients will be in the Gradient coordinate system. Absolute: Outputs the "
                   "absolute coefficient by taking into account the current shim settings. This is effectively "
                   "initial + shim. Scanner coil coefficients will be in the Shim coordinate system.")
@click.option('-v', '--verbose', type=click.Choice(['info', 'debug']), default='info', help="Be more verbose")
def realtime_cli(fname_fmap, fname_anat, fname_mask_anat_static, fname_mask_anat_riro, fname_resp, method, slices,
                 slice_factor, coils, dilation_kernel_size, scanner_coil_order, fname_sph_constr,
                 path_output, o_format, output_value_format, verbose):
    """ Realtime shim by fitting a fieldmap to a pressure monitoring unit. Use the option --optimizer-method to change
    the shimming algorithm used to optimize. Use the options --slices and --slice-factor to change the shimming
    order/size of the slices.

    Example of use: st_b0shim realtime --coil coil1.nii coil1_config.json --coil coil2.nii coil2_config.json
    --fmap fmap.nii --anat anat.nii --mask-static mask.nii --resp trace.resp --optimizer-method least_squares
    """

    # Error out for unsupported inputs. File format is in gradient CS, adding gradient CS to Shim CS does not work
    if output_value_format == 'absolute' and o_format == 'eva':
        raise ValueError(f"Unsupported output value format: {output_value_format} for output file format: {o_format}")

    # Input can be a string
    scanner_coil_order = int(scanner_coil_order)

    # Set logger level
    set_all_loggers(verbose)

    # Prepare the output
    create_output_dir(path_output)

    # Load the fieldmap, expand the dimensions of the fieldmap if one of the dimensions is 2 or less. This is done since
    # we are fitting a fieldmap to coil profiles, having essentially a 2d matrix as a fieldmap can lead to errors in the
    # through plane direction.
    fmap_required_dims = 4
    nii_fmap = _load_fmap(fname_fmap, fmap_required_dims, dilation_kernel_size, path_output)

    # Load the anat
    nii_anat = nib.load(fname_anat)
    dim_info = nii_anat.header.get_dim_info()
    if dim_info[2] != 2:
        # Slice must be the 3rd dimension of the file
        # TODO: Reorient nifti so that the slice is the 3rd dim
        raise RuntimeError("Slice encode direction must be the 3rd dimension of the NIfTI file.")

    # Load static mask
    if fname_mask_anat_static is not None:
        nii_mask_anat_static = nib.load(fname_mask_anat_static)
    else:
        # If no mask is provided, shim the whole anat volume
        nii_mask_anat_static = nib.Nifti1Image(np.ones_like(nii_anat.get_fdata()), nii_anat.affine,
                                               header=nii_anat.header)

    # Load riro mask
    if fname_mask_anat_riro is not None:
        nii_mask_anat_riro = nib.load(fname_mask_anat_riro)
    else:
        # If no mask is provided, shim the whole anat volume
        nii_mask_anat_riro = nib.Nifti1Image(np.ones_like(nii_anat.get_fdata()), nii_anat.affine,
                                             header=nii_anat.header)

    # Open json of the fmap
    fname_json = fname_fmap.split('.nii')[0] + '.json'
    # Read from json file
    if os.path.isfile(fname_json):
        json_fm_data = json.load(open(fname_json))
    else:
        raise OSError("Missing fieldmap json file")

    # Get the initial coefficients from the json file (Tx + 1st + 2nd order shim)
    json_coefs = _get_current_shim_settings(json_fm_data)
    converted_coefs = convert_to_mp(json_coefs[1:], json_fm_data['ManufacturersModelName'])
    initial_coefs = [json_coefs[0]] + converted_coefs

    # Load the coils
    list_coils = _load_coils(coils, scanner_coil_order, fname_sph_constr, nii_fmap, initial_coefs)

    if logger.level <= getattr(logging, 'DEBUG'):
        # Save inputs
        list_fname = [fname_fmap, fname_anat, fname_mask_anat_static, fname_mask_anat_riro]
        _save_nii_to_new_dir(list_fname, path_output)

    # Get the shim slice ordering
    n_slices = nii_anat.shape[2]
    list_slices = define_slices(n_slices, slice_factor, slices)
    logger.info(f"The slices to shim are: {list_slices}")

    # Load PMU
    pmu = PmuResp(fname_resp)

    out = shim_realtime_pmu_sequencer(nii_fmap, json_fm_data, nii_anat, nii_mask_anat_static, nii_mask_anat_riro,
                                      list_slices, pmu, list_coils,
                                      opt_method=method,
                                      mask_dilation_kernel='sphere',
                                      mask_dilation_kernel_size=dilation_kernel_size,
                                      path_output=path_output)

    coefs_static, coefs_riro, mean_p, p_rms = out

    list_fname_output = []
    end_channel = 0
    for i_coil, coil in enumerate(list_coils):

        # Figure out the start and end channels for a coil to be able to select it from the coefs
        n_channels = coil.dim[3]
        start_channel = end_channel
        end_channel = start_channel + n_channels

        # Select the coefficients for a coil
        coefs_coil_static = copy.deepcopy(coefs_static[:, start_channel:end_channel])
        coefs_coil_riro = copy.deepcopy(coefs_riro[:, start_channel:end_channel])

        # If it's a scanner
        if type(coil) == ScannerCoil:

            if output_value_format == 'delta' and scanner_coil_order >= 1:
                # TODO: Fix for 2nd order (must validate 2nd order siemens basis)
                logger.debug("Converting scanner coil from Physical CS (RAS) to Gradient CS")

                coefs_st_freq, coefs_st_phase, coefs_st_slice = phys_to_gradient_cs(
                    coefs_coil_static[:, 1],
                    coefs_coil_static[:, 2],
                    coefs_coil_static[:, 3],
                    fname_anat)
                coefs_coil_static[:, 1] = coefs_st_freq
                coefs_coil_static[:, 2] = coefs_st_phase
                coefs_coil_static[:, 3] = coefs_st_slice

                coefs_riro_freq, coefs_riro_phase, coefs_riro_slice = phys_to_gradient_cs(
                    coefs_coil_riro[:, 1],
                    coefs_coil_riro[:, 2],
                    coefs_coil_riro[:, 3],
                    fname_anat)
                coefs_coil_riro[:, 1] = coefs_riro_freq
                coefs_coil_riro[:, 2] = coefs_riro_phase
                coefs_coil_riro[:, 3] = coefs_riro_slice

                # # Plot a figure of the coefficients, order 0 is in Hz, order 1 in mt/m, order 2 in mt/m^2
                # units = "Gradient CS [mT/m]"
                # _plot_coefs(coil, list_slices, coefs_coil_static, path_output, i_coil, coefs_coil_riro,
                #             pres_probe_max=pmu.max - mean_p, pres_probe_min=pmu.min - mean_p, units=units)

            else:  # output_value_format == 'absolute'
                # Load anat json
                fname_anat_json = fname_anat.rsplit('.nii', 1)[0] + '.json'
                with open(fname_anat_json) as json_file:
                    json_anat_data = json.load(json_file)

                if json_anat_data['Manufacturer'] == 'Siemens':
                    # Change from RAS to LAI (ShimCS)
                    # x
                    coefs_coil_static[:, 1] = -coefs_coil_static[:, 1]
                    coefs_coil_riro[:, 1] = -coefs_coil_riro[:, 1]
                    # z
                    coefs_coil_static[:, 3] = -coefs_coil_static[:, 3]
                    coefs_coil_riro[:, 3] = -coefs_coil_riro[:, 3]

                    # Bounds also change from RAS to LAI
                    bounds_shim_cs = np.array(coil.coef_channel_minmax)
                    bounds_shim_cs[1] = -bounds_shim_cs[1]
                    bounds_shim_cs[3] = -bounds_shim_cs[3]
                else:
                    raise NotImplementedError(f"Manufacturer: {json_anat_data['Manufacturer']} not yet implemented for"
                                              f"absolute format")

                # # Plot a figure of the coefficients, order 0 is in Hz, order 1 in mt/m, order 2 in mt/m^2
                # units = "ShimCS [mT/m]"
                # _plot_coefs(coil, list_slices, coefs_coil_static, path_output, i_coil, coefs_coil_riro,
                #             pres_probe_max=pmu.max - mean_p, pres_probe_min=pmu.min - mean_p, units=units,
                #             bounds=bounds_shim_cs)

                for i_channel in range(n_channels):
                    # abs_coef = delta + initial
                    coefs_coil_static[:, i_channel] = coefs_coil_static[:, i_channel] + initial_coefs[i_channel]
                    # riro does not change

        else:  # Custom coil
            # Plot a figure of the coefficients
            _plot_coefs(coil, list_slices, coefs_coil_static, path_output, i_coil, coefs_coil_riro,
                        pres_probe_max=pmu.max - mean_p, pres_probe_min=pmu.min - mean_p,
                        bounds=coil.coef_channel_minmax)

        list_fname_output += _save_to_text_file_rt(coil, coefs_coil_static, coefs_coil_riro, mean_p, list_slices,
                                                   path_output, o_format, i_coil)

    logger.info(f"Coil txt file(s) are here:\n{os.linesep.join(list_fname_output)}")


def _save_to_text_file_rt(coil, currents_static, currents_riro, mean_p, list_slices, path_output, o_format,
                          coil_number):
    """o_format can either be 'chronological-ch', 'chronological-coil'"""

    list_fname_output = []
    n_channels = coil.dim[3]

    # o_format[-3:] == '-ch':
    # Write a file for each channel
    for i_channel in range(n_channels):
        fname_output = os.path.join(path_output, f"coefs_coil{coil_number}_ch{i_channel}_{coil.name}.txt")

        if o_format == 'chronological-ch':
            with open(fname_output, 'w', encoding='utf-8') as f:
                # Each row will have 3 coef representing the static, riro and mean_p in chronological order
                for i_shim in range(len(list_slices)):
                    f.write(f"{currents_static[i_shim, i_channel]:.6f}, ")
                    f.write(f"{currents_riro[i_shim, i_channel]:.12f}, ")
                    f.write(f"{mean_p:.4f}\n")

        elif o_format == 'slicewise-ch':
            with open(fname_output, 'w', encoding='utf-8') as f:
                # Each row will have one coef representing the static, riro and mean_p in slicewise order
                n_slices = np.sum([len(a_tuple) for a_tuple in list_slices])
                for i_slice in range(n_slices):
                    i_shim = [list_slices.index(i) for i in list_slices if i_slice in i][0]
                    f.write(f"{currents_static[i_shim, i_channel]:.6f}, ")
                    f.write(f"{currents_riro[i_shim, i_channel]:.12f}, ")
                    f.write(f"{mean_p:.4f}\n")

        # TODO: Remove once implemented in more streamlined way
        else:  # o_format == 'eva':

            # Make sure there are 4 channels
            if n_channels != 4:
                raise RuntimeError("Eva's output format should only be used with 1st order scanner coils")

            name = {0: 'f0',
                    1: 'x',
                    2: 'y',
                    3: 'z'}

            fname_output = os.path.join(path_output, f"{name[i_channel]}shim_gradients.txt")
            with open(fname_output, 'w', encoding='utf-8') as f:
                n_slices = np.sum([len(a_tuple) for a_tuple in list_slices])
                for i_slice in range(n_slices):
                    i_shim = [list_slices.index(i) for i in list_slices if i_slice in i][0]

                    if i_channel == 0:
                        # f0, Output is in Hz
                        f.write(f"corr_vec[0][{i_slice}]= "
                                f"{currents_static[i_shim, i_channel]:.6f}\n")
                        f.write(f"corr_vec[1][{i_slice}]= "
                                f"{currents_riro[i_shim, i_channel]:.12f}\n")
                        f.write(f"corr_vec[2][{i_slice}]= {mean_p:.3f}\n")

                    else:
                        # For Gx, Gy, Gz: Divide by 1000 for mt/m
                        f.write(f"corr_vec[0][{i_slice}]= "
                                f"{currents_static[i_shim, i_channel] / 1000:.6f}\n")
                        f.write(f"corr_vec[1][{i_slice}]= "
                                f"{currents_riro[i_shim, i_channel] / 1000:.12f}\n")
                        f.write(f"corr_vec[2][{i_slice}]= {mean_p:.3f}\n")

        list_fname_output.append(os.path.abspath(fname_output))

    return list_fname_output


def _load_fmap(fname_fmap, n_dims, dilation_kernel_size, path_output):
    """ Load the fmap and expand its dimensions to the kernel size

    Args:
        fname_fmap (str): Filename of the fieldmap
        n_dims (int): Number of dimensions of the fieldmap (3 or 4)
        dilation_kernel_size: Size of the kernel

    Returns:
        nibabel.Nifti1Image: Nibabel object of the loaded and extended fieldmap

    """
    # Load the fieldmap
    nii_fmap_orig = nib.load(fname_fmap)

    # Make sure the fieldmap has the appropriate dimensions.
    if nii_fmap_orig.get_fdata().ndim != n_dims:
        raise ValueError(f"Fieldmap must be {n_dims}")

    # Extend the fieldmap if there are axes that are 1d. This is done since we are fitting a fieldmap to coil profiles,
    # having essentially a 2d matrix as a fieldmap can lead to errors in the through plane direction. To metigate this,
    # we create a 3d volume by replicating the single slice.
    if 1 in nii_fmap_orig.shape[:3]:
        n_slices_to_expand = int(math.ceil((dilation_kernel_size - 1) / 2))
        fieldmap_shape = nii_fmap_orig.shape
        # Find the list of axes that has a length of 1
        list_axis = [i for i in range(3) if fieldmap_shape[i] == 1]

        # Extend for each axes
        tmp_nii = nii_fmap_orig
        for i_axis in list_axis:
            tmp_nii = extend_slice(tmp_nii, n_slices=n_slices_to_expand, axis=i_axis)
        nii_fmap = tmp_nii

        # If DEBUG, save the extended fieldmap
        if logger.level <= getattr(logging, 'DEBUG'):
            fname_new_fmap = os.path.join(path_output, 'tmp_extended_fmap.nii.gz')
            nib.save(nii_fmap, fname_new_fmap)
            logger.debug(f"Extended fmap, saved the new fieldmap here: {fname_new_fmap}")

    else:
        # Load the original
        nii_fmap = nii_fmap_orig

    return nii_fmap


def _load_coils(coils, order, fname_constraints, nii_fmap, initial_coefs):
    """ Loads the Coil objects from filenames

    Args:
        coils (list): List of tuples(fname_nii, fname_json) of coil profiles and constraints
        order (int): Order of the scanner coils (0 or 1 or 2)
        fname_constraints (str): Filename of the constraints of the scanner coils
        nii_fmap (nib.Nifti1Image): Nibabel object of the fieldmap
        initial_coefs (list): List of coefficients corresponding to the scanner coil.

    Returns:
        list: List of Coil objects containing the custom coils followed by the scanner coil if requested
    """
    list_coils = []

    # Load custom coils
    for coil in coils:
        nii_coil_profiles = nib.load(coil[0])
        constraints = json.load(open(coil[1]))
        list_coils.append(Coil(nii_coil_profiles.get_fdata(), nii_coil_profiles.affine, constraints))

    # Create the spherical harmonic coil profiles of the scanner
    if 0 <= order <= 2:

        if os.path.isfile(fname_constraints):
            sph_contraints = json.load(open(fname_constraints))

            def _initial_in_bounds(coefs, bounds):
                """Makes sure the initial values are within the bounds of the constraints"""
                if len(coefs) != len(bounds):
                    raise RuntimeError("The scanner coil's bounds is not the same length as the initial bounds found "
                                       "in the json")
                for i_bound in range(len(bounds)):
                    if not (bounds[i_bound][0] <= coefs[i_bound] <= bounds[i_bound][1]):
                        raise RuntimeError(f"Initial scanner coefs are outside the bounds allowed in the constraints: "
                                           f"{bounds[i_bound]}, initial: {coefs[i_bound]}")

            _initial_in_bounds(initial_coefs, sph_contraints['coef_channel_minmax'])
            # Set the bounds to what they should be by taking into account that the fieldmap was acquired using some
            # shimming
            sph_contraints['coef_channel_minmax'] = new_bounds_from_currents(np.array([initial_coefs]),
                                                                             sph_contraints['coef_channel_minmax'])[0]
        else:
            raise OSError("Missing json file")

        # Create a ScannerCoil object
        scanner_coil = ScannerCoil('ras', nii_fmap.shape[:3], nii_fmap.affine, sph_contraints, order)
        list_coils.append(scanner_coil)

    # Make sure a coil is selected
    if len(list_coils) == 0:
        raise RuntimeError("No custom or scanner coils were selected. Use --coil and/or --scanner-coil-order")

    return list_coils


def _save_nii_to_new_dir(list_fname, path_output):
    """List of nii to save to a new output folder"""
    logger.debug(f"Saving CLI inputs to: {path_output}")
    for fname in list_fname:
        if fname is None:
            continue
        nii = nib.load(fname)
        fname_to_save = os.path.join(path_output, os.path.basename(fname))
        nib.save(nii, fname_to_save)


@click.command(context_settings=CONTEXT_SETTINGS)
@click.option('--slices', required=True,
              help="Enter the total number of slices. Also accepts a path to an anatomical file to determine the "
                   "number of slices automatically. (Looks at 3rd dim)")
@click.option('--factor', required=True, type=click.INT,
              help="Number of slices per shim")
@click.option('--method', type=click.Choice(['interleaved', 'sequential', 'volume']), required=True,
              help="Defines how the slices should be sorted")
@click.option('-o', '--output', 'fname_output', type=click.Path(), default=os.path.join(os.curdir, 'slices.json'),
              show_default=True, help="Output filename for the json file")
def define_slices_cli(slices, factor, method, fname_output):
    """ Define slices to shim to a json file according to the number slices, factor and method used.

    """
    # Get the number of slices
    click.echo(type(slices))
    if os.path.isfile(slices):
        nii_anat = nib.load(slices)
        n_slices = nii_anat.shape[2]
    else:
        try:
            n_slices = int(slices)
        except ValueError:
            raise ValueError(f"Could not get the number of slices. Make sure {slices} is a number or a file that "
                             f"exists")

    list_slices = define_slices(n_slices, factor, method)

    if fname_output[-5:] != '.json':
        raise ValueError("Filename of the output must be a json file")
    create_output_dir(fname_output, is_file=True)

    with open(fname_output, 'w', encoding='utf-8') as f:
        json.dump(list_slices, f, ensure_ascii=False, indent=4)

    logger.info(f"The slices to shim are: {list_slices}")


def _get_current_shim_settings(json_data):
    # Get the current coefficients of the spherical harmonics coil profiles
    current_coefs = json_data['ShimSetting']
    f0 = json_data['ImagingFrequency'] * 1e6
    # Tx (1) + 1st order (3) + 2nd order (5)
    current_coefs.insert(0, int(f0))

    return current_coefs


def _plot_coefs(coil, slices, static_coefs, path_output, coil_number, rt_coefs=None, pres_probe_min=None,
                pres_probe_max=None, units='', bounds=None):
    n_shims = static_coefs.shape[0]
    fig = Figure(figsize=(8, 4 * n_shims), tight_layout=True)

    # Find min and max values of the plots
    # Calculate the min and max of the bounds if its an input
    if bounds is not None:
        bounds = np.array(bounds)
        min_y = bounds.min()
        max_y = bounds.max()
    else:
        min_y = None
        max_y = None

    # Calculate the min and max coefficient for the combined static + riro * (acq_pressure - mean_p)
    # It can expand the min/max of the bounds if necessary
    if rt_coefs is not None:
        for i_shim in range(n_shims):
            n_channels = static_coefs.shape[1]
            for i_channel in range(n_channels):
                coef = rt_coefs[i_shim, i_channel]
                if coef > 0:
                    temp_min = static_coefs[i_shim, i_channel] + coef * pres_probe_min
                    temp_max = static_coefs[i_shim, i_channel] + coef * pres_probe_max
                else:
                    temp_min = static_coefs[i_shim, i_channel] + coef * pres_probe_max
                    temp_max = static_coefs[i_shim, i_channel] + coef * pres_probe_min

                if min_y is None or min_y > temp_min:
                    min_y = temp_min
                if max_y is None or max_y < temp_max:
                    max_y = temp_max

    # If its static optimization, find the min and max. It can expand the bounds.
    else:
        temp_min = np.array(static_coefs).min()
        if min_y is None or min_y > temp_min:
            min_y = temp_min
        temp_max = np.array(static_coefs).max()
        if max_y is None or max_y < temp_max:
            max_y = np.array(static_coefs).max()

    # Create a plot for each shim group
    for i_shim in range(n_shims):
        ax = fig.add_subplot(n_shims + 1, 1, i_shim + 1)
        n_channels = static_coefs.shape[1]

        # Add realtime component as an errorbar
        if rt_coefs is not None:
            rt_coef_ishim = rt_coefs[i_shim]
            riro = [rt_coef_ishim * -pres_probe_min, rt_coef_ishim * pres_probe_max]
            ax.errorbar(range(n_channels), static_coefs[i_shim], yerr=riro, fmt='o', elinewidth=4, capsize=6,
                        label='static-riro')
        # Add static component
        else:
            ax.scatter(range(n_channels), static_coefs[i_shim], marker='o', label='static')

        # Draw a black line at y=0
        ax.hlines(0, 0, 1, transform=ax.get_yaxis_transform(), colors='k')

        delta_y = max_y - min_y

        # Add bounds on the graph
        if bounds is not None:
            # Channel 0 used for the legend
            len_vline_bounds = 0.01
            len_hline_bounds = 0.4
            # min
            ax.hlines(bounds[0, 0], -len_hline_bounds, len_hline_bounds, colors='r', label='bounds',
                      capstyle='projecting')
            ax.vlines(-len_hline_bounds, bounds[0, 0], bounds[0, 0] + (delta_y * len_vline_bounds), colors='r',
                      capstyle='projecting')
            ax.vlines(len_hline_bounds, bounds[0, 0], bounds[0, 0] + (delta_y * len_vline_bounds), colors='r',
                      capstyle='projecting')
            # max
            ax.hlines(bounds[0, 1], -len_hline_bounds, len_hline_bounds, colors='r', capstyle='projecting')
            ax.vlines(-len_hline_bounds, bounds[0, 1] - (delta_y * len_vline_bounds), bounds[0, 1], colors='r',
                      capstyle='projecting')
            ax.vlines(len_hline_bounds, bounds[0, 1] - (delta_y * len_vline_bounds), bounds[0, 1], colors='r',
                      capstyle='projecting')
            # All other channels
            for i_channel in range(1, n_channels):
                # min
                ax.hlines(bounds[i_channel, 0], i_channel - len_hline_bounds, i_channel + len_hline_bounds, colors='r',
                          capstyle='projecting')
                ax.vlines(i_channel - len_hline_bounds, bounds[i_channel, 0],
                          bounds[i_channel, 0] + (delta_y * len_vline_bounds), colors='r', capstyle='projecting')
                ax.vlines(i_channel + len_hline_bounds, bounds[i_channel, 0],
                          bounds[i_channel, 0] + (delta_y * len_vline_bounds), colors='r', capstyle='projecting')
                # max
                ax.hlines(bounds[i_channel, 1], i_channel - len_hline_bounds, i_channel + len_hline_bounds, colors='r',
                          capstyle='projecting')
                ax.vlines(i_channel - len_hline_bounds, bounds[i_channel, 1] - (delta_y * len_vline_bounds),
                          bounds[i_channel, 1], colors='r', capstyle='projecting')
                ax.vlines(i_channel + len_hline_bounds, bounds[i_channel, 1] - (delta_y * len_vline_bounds),
                          bounds[i_channel, 1], colors='r', capstyle='projecting')

        # Set the extent of the plot
        ax.set(ylim=(min_y - (0.05 * delta_y), max_y + (0.05 * delta_y)), xlim=(-0.75, n_channels - 0.25),
               xticks=range(n_channels))
        ax.legend()
        ax.set_title(f"Slices: {slices[i_shim]}")
        ax.set_xlabel('Channels')
        ax.set_ylabel(f"Coefficients {units}")

    fname_figure = os.path.join(path_output, f"fig_currents_per_slice_group_coil{coil_number}_{coil.name}.png")
    fig.savefig(fname_figure, bbox_inches='tight')
    logger.debug(f"Saved figure: {fname_figure}")


b0shim_cli.add_command(realtime_shim_cli, 'gradient_realtime')
b0shim_cli.add_command(static_cli, 'static')
b0shim_cli.add_command(realtime_cli, 'realtime')
# shim_cli.add_command(define_slices_cli, 'define_slices')