#!/usr/bin/env python

#----------------------------------------------------------------------
# Copyright (c) 2013 Raytheon BBN Technologies
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and/or hardware specification (the "Work") to
# deal in the Work without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Work, and to permit persons to whom the Work
# is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Work.
#
# THE WORK IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE WORK OR THE USE OR OTHER DEALINGS
# IN THE WORK.
#----------------------------------------------------------------------

import copy
import json
import logging
import os
import random
import string
import time
from xml.dom.minidom import parseString, Node as XMLNode

from GENIObject import *
from VLANRange import *
import RSpecParser

from omnilib.util.handler_utils import _construct_output_filename, _writeRSpec, _getRSpecOutput, _printResults
from geni.util import rspec_schema, rspec_util

# FIXME: As in RSpecParser, check use of getAttribute vs getAttributeNS and localName vs nodeName

class Path(GENIObject):
    '''Path'''
    __ID__ = validateText
#    __simpleProps__ = [ ['id', int] ]

    # XML tag constants
    ID_TAG = 'id'
    HOP_TAG = 'hop'

    @classmethod
    def fromDOM(cls, element):
        """Parse a stitching path from a DOM element."""
        # FIXME: Do we need getAttributeNS?
        id = element.getAttribute(cls.ID_TAG)
        path = Path(id)
        for child in element.childNodes:
            if child.localName == cls.HOP_TAG:
                hop = Hop.fromDOM(child)
                hop.path = path
                hop.idx = len(path.hops)
                path.hops.append(hop)
        for hop in path.hops:
            next_hop = path.find_hop(hop._next_hop)
            if next_hop:
                hop._next_hop = next_hop
        return path

    def __init__(self, id):
        super(Path, self).__init__()
        self.id = id
        self._hops = []
        self._aggregates = set()

    @property
    def hops(self):
        return self._hops

    @property
    def aggregates(self):
        return self._aggregates

    @hops.setter
    def hops(self, hopList):
#DELETE        self._setListProp('hops', hopList, Hop, '_path')
        self._setListProp('hops', hopList, Hop)

    def find_hop(self, hop_urn):
        for hop in self.hops:
            if hop.urn == hop_urn:
                return hop
        # Fail -- no hop matched the given URN
        return None

    def find_hop_idx(self, hop_idx):
        '''Find a hop in this path by its index, or None'''
        for hop in self.hops:
            if hop.idx == hop_idx:
                return hop
        # Fail -- no hop matched the given index
        return None

    def editChangesIntoDom(self, pathDomNode):
        '''Edit any changes made in this element into the given DomNode'''
        # Note the parent RSpec element's dom is not touched, unless the given node is from that document
        # Here we just find all the Hops and let them do stuff

        # Incoming node should be the node for this path
        nodeId = pathDomNode.getAttribute(self.ID_TAG)
        if nodeId != self.id:
            raise StitchingError("Path %s given Dom node with different Id: %s" % (self, nodeId))

        # For each of this path's hops, find the appropriate Dom element, and let Hop edit itself in
        domHops = pathDomNode.getElementsByTagName(self.HOP_TAG)
        for hop in self.hops:
            domHopNode = None
            if domHops:
                for hopNode in domHops:
                    hopNodeId = hopNode.getAttribute(self.ID_TAG)
                    if hopNodeId == hop._id:
                        domHopNode = hopNode
                        break
            if domHopNode is None:
                # Couldn't find this Hop in the dom
                # FIXME: Create it?
                raise StitchingError("Couldn't find Hop %s in given Dom node to edit in changes" % hop)
            hop.editChangesIntoDom(domHopNode)
        # End of loop over hops
        return

class Stitching(GENIObject):
    __simpleProps__ = [ ['last_update_time', str] ] #, ['path', Path[]]]

    def __init__(self, last_update_time=None, paths=None):
        super(Stitching, self).__init__()
        self.last_update_time = str(last_update_time)
        self.paths = paths

    # Arg of link_id: this is the client_id of the main body link, or the path ID
    def find_path(self, link_id):
        if self.paths:
            for path in self.paths:
                if path.id == link_id:
                    return path
        else:
            return None


class Aggregate(object):
    '''Aggregate'''

    # Hold all instances. One instance per URN.
    aggs = dict()

    @classmethod
    def find(cls, urn):
        if not urn in cls.aggs:
            m = cls(urn)
            cls.aggs[urn] = m
        return cls.aggs[urn]

    @classmethod
    def all_aggregates(cls):
        return cls.aggs.values()

    def __init__(self, urn, url=None):
        self.urn = urn
        self.url = url
        self.inProcess = False
        self.completed = False
        self.userRequested = False
        self._hops = set()
        self._paths = set()
        self._dependsOn = set()
        self.isDependencyFor = set() # AMs that depend on this: for ripple down deletes
        self.logger = logging.getLogger('stitch.Aggregate')
        # Note these are sort of RSpecs but not RSpec objects, to avoid a loop
        self.requestDom = None # the DOM as constructed to submit in request to this AM
        self.manifestDom = None # the DOM as we got back from the AM
        self.api_version = 2 # FIXME: Set this from stitchhandler.parseSCSResponse
        self.dcn = False # DCN AMs require waiting for sliverstatus to say ready before the manifest is legit
        # FIXME: # reservation tries since last call to SCS?

    def __str__(self):
        return "<Aggregate %s>" % (self.urn)

    def __repr__(self):
        return "Aggregate(%r)" % (self.urn)

    @property
    def hops(self):
        return list(self._hops)

    @property
    def paths(self):
        return list(self._paths)

    @property
    def dependsOn(self):
        return list(self._dependsOn)

    def add_hop(self, hop):
        self._hops.add(hop)

    def add_path(self, path):
        self._paths.add(path)

    def add_dependency(self, agg):
        self._dependsOn.add(agg)

    def add_agg_that_dependsOnThis(self, agg):
        self.isDependencyFor.add(agg)

    @property
    def dependencies_complete(self):
        """Dependencies are complete if there are no dependencies
        or if all dependencies are completed.
        """
        return (not self._dependsOn
                or reduce(lambda a, b: a and b,
                          [agg.completed for agg in self._dependsOn]))

    @property
    def ready(self):
        return not self.completed and not self.inProcess and self.dependencies_complete

    def allocate(self, opts, slicename, rspecDom):
        if self.inProcess:
            self.logger.warn("Called allocate on AM already in process: %s", self)
            return
        # Confirm all dependencies still done
        if not self.dependencies_complete:
            self.logger.warn("Cannot allocate AM %s: dependencies not ready", self)
            return
        if self.completed:
            self.logger.warn("Called allocate on AM already maked complete", self)
            return

        # FIXME: If we are quitting, return (important when threaded)

        # Import VLANs, noting if we need to delete an old reservation at this AM first
        mustDelete, alreadyDone = self.copyVLANsAndDetectRedo()

        if mustDelete:
            self.logger.warn("Must delete previous reservation for AM %s", self)
            alreadyDone = False
            self.deleteReservation(opts, slicename)
        # end of block to delete a previous reservation

        if alreadyDone:
            # we did a previous upstream delete and worked our way down to here, but this AM is OK
            self.completed = True
            self.logger.info("AM %s had previous result we didn't need to redo. Done", self)
            return

        # FIXME: Check for all hops have reasonable vlan inputs?

        self.completed = False

        # Mark AM is busy
        self.inProcess = True

        # Generate the new request Dom
        self.requestDom = self.getEditedRSpecDom(rspecDom)

        # Get the manifest for this AM
        # result is a manifest RSpec string or a dict with the error if any
        # success is a boolean
        # This method handles fakeMode, retrying on BUSY, polling SliverStatus for DCN AMs
        (result, success) = self.doReservation(opts, slicename)

        if success and result:
            # FIXME: Handle ION needing me to do sliverstatus first
            #  Do we special case the ION & ? AMs that require this? How ID those?
            #  Do we wait a # of tries on sliverstatus? # of minutes?
            if self.dcn:
                self.logger.warn("Need to poll sliverstatus for this DCN AM")
            #  if ION:
            #   while (keep checking):
            #     call sliverStatus
            #     If failed or ready, break
            #     If error doing the call, break?
            #   If failed:
            #     success = False
            #     result = ??
            #     get to the else somehow?
            #   else:
            #     call ListResources
            #     get the result per above

            # Now we have a manifest

            # Save it on the Agg
            try:
                self.manifestDom = parseString(result)
            except Exception, e:
                self.logger.error("Failed to parse result as DOM XML RSpec: %s", e)
                # FIXME: Handle error

            # Parse out the VLANs we got, saving them away on the HopLinks
            # Note and complain if we didn't get VLANs or the VLAN we got is not what we requested
            for hop in self.hops:
                # FIXME: Hop ID is not specific enough. Need a path ID as well
                range_suggested = self.getVLANRangeSuggested(self.manifestDom, hop._id)
                rangeValue = range_suggested[0]
                suggestedValue = range_suggested[1]
                if not suggestedValue:
                    self.logger.warn("Didn't find suggested value for hop %s", hop)
                    raise StitchingError("%s didn't have a suggestedVlanRange in manifest" % hop)
                elif suggestedValue in ('null', 'None', 'any'):
                    self.logger.error("Hop %s Suggested invalid: %s", hop, suggestedValue)
                    raise StitchingError("%s had invalidsuggestedVlanRange in manifest: %s" % (hop, suggestedValue))
                else:
                    suggestedObject = VLANRange.fromString(suggestedValue)
                if not rangeValue:
                    self.logger.warn("Didn't find vlanAvailRange element for hop %s", hop)
                    raise StitchingError("%s didn't have a vlanAvailRange in manifest" % hop)
                else:
                    rangeObject = VLANRange.fromString(rangeValue)

                self.logger.debug("Hop %s manifest had suggested %s, avail %s", hop, suggestedValue, rangeValue)
                if not suggestedObject <= hop._hop_link.vlan_suggested_request:
                    self.logger.error("AM %s gave VLAN %s for hop %s which is not in our request %s", self, suggestedObject, hop, hop._hop_link.vlan_suggested_request)
                    # FIXME: Handle error here
                    # This is sug != requested case
                hop._hop_link.vlan_suggested_manifest = suggestedObject
                hop._hop_link.vlan_range_manifest = rangeObject

        else:
            # Handle got a struct with an error code. If that code is VLAN_UNAVAILABLE, do one thing.
            # else if got BUSY, retry (after a pause)
            # else, do another

            # FIXME FIXME
            self.logger.error("Got error from %s: %s %s", self, text, result)

            # FIXME: See amhandler._retrieve_value

            pass

        # Mark AM not busy
        self.inProcess = False

        self.logger.info("Allocation at %s complete", self)

        # mark self complete
        self.completed = True

    # Take a hop element and return a tuple (vlanRangeAvailability, suggestedVLANRange)
    # FIXME: Hop ID is not enough. Need a path ID as well.
    def getVLANRangeSuggested(self, manifest, hop_id):
        vlan_range_availability = None
        suggested_vlan_range = None

        rspec_node = None
        stitching_node = None
        hop_node = None
        scd_node = None
        scsi_node = None

        # FIXME: Use constants for these strings used here to find the proper elements

        for child in manifest.childNodes:
            if child.nodeType == XMLNode.ELEMENT_NODE and \
                    child.nodeName == 'rspec':
                rspec_node = child
                break

        if rspec_node:
            for child in rspec_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.nodeName == 'stitching':
                    stitching_node = child
                    break
        else:
            raise StitchingError("No rspec element in manifest")

        if stitching_node:
            # FIXME: There can be multiple <path> elements for a single rspec.
            # Distinguish by path ID
            path_node = stitching_node.childNodes[0]
            for child in path_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.nodeName == 'hop':
                    this_hop_id = child.getAttribute('id')
                    if this_hop_id == hop_id:
                        hop_node = child
                        break
        else:
            raise StitchingError("No stitching element in manifest")
                
        if hop_node:
            link_node = hop_node.childNodes[0]
            for child in link_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.nodeName == 'switchingCapabilityDescriptor':
                    scd_node = child
                    break;
        else:
            raise StitchingError("Couldn't find hop %s in rspec", hop_id)

        if scd_node:
            for child in scd_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.nodeName == 'switchingCapabilitySpecificInfo':
                    scsi_node = child;
                    break
        else:
            raise StitchingError("Couldn't find switchingCapabilityDescriptor in hop %s in rspec", hop_id)

        if scsi_node:
            # FIXME: There will shortly be other kinds of sub-tags here. Need to look for this explicitly
            scsil2_node = scsi_node.childNodes[0]
            for child in scsil2_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE:
                    child_text = child.childNodes[0].nodeValue
                    if child.nodeName == 'vlanRangeAvailability':
                        vlan_range_availability = child_text
                    elif child.nodeName == 'suggestedVLANRange':
                        suggested_vlan_range = child_text
        else:
            raise StitchingError("Couldn't find switchingCapabilitySpecificInfo in hop %s in rspec", hop_id)

        return (vlan_range_availability, suggested_vlan_range)


    def getEditedRSpecDom(self, originalRSpec):
        # For each path on this AM, get that Path to write whatever it thinks necessary into a
        # deep clone of the incoming RSpec Dom
        requestRSpecDom = originalRSpec.cloneNode(True)
        stitchNodes = requestRSpecDom.getElementsByTagName(RSpecParser.STITCHING_TAG)
        if stitchNodes and len(stitchNodes) > 0:
            stitchNode = stitchNodes[0]
        else:
            raise StitchingError("Couldn't find stitching element in rspec")

        domPaths = stitchNode.getElementsByTagName(RSpecParser.PATH_TAG)
#        domPaths = stitchNode.getElementsByTagNameNS(rspec_schema.STITCH_SCHEMA_V1, RSpecParser.PATH_TAG)
        for path in self.paths:
            self.logger.debug("Looking for node for path %s", path)
            domNode = None
            if domPaths:
                for pathNode in domPaths:
                    pathNodeId = pathNode.getAttribute(Path.ID_TAG)
                    if pathNodeId == path.id:
                        domNode = pathNode
                        self.logger.debug("Found node for path %s", path.id)
                        break
            if domNode is None:
                raise StitchingError("Couldn't find Path %s in stitching element of RSpec" % path)
            self.logger.debug("Doing path.editChanges for path %s", path.id)
            path.editChangesIntoDom(domNode)
        return requestRSpecDom

    def doReservation(self, opts, slicename):
        # Ensure we have the right URL / API version / command combo
        # If this AM does APIv3, I'd like to use it
        # But the caller needs to know if we used APIv3 so they know whether to call provision later
        opName = 'createsliver'
        if self.api_version > 2:
            opName = 'allocate'

        # Write the request rspec to a string that we save to a file
#        # FIXME: Put this file in /tmp? If not in fakeMode, delete when done?
        # Careful - this is a request. Don't make it look like a manifest
#        retVal, rspecfileName = _writeRSpec(opts, self.logger, self.requestDom.toprettyxml(), slicename, self.urn, self.url)
        # FIXME: the header says it is reserved resources. Is this worse? Make my own header instead?
#        (header, content, retVal) = _getRSpecOutput(self.logger, self.requestDom.toprettyxml(), slicename, self.urn, self.url, None)
        requestString = self.requestDom.toprettyxml()
        header = "<!-- Resource request for stitching for:\n\tSlice: %s\n\t at AM:\n\tURN: %s\n\tURL: %s\n -->" % (slicename, self.urn, self.url)
        if requestString and rspec_util.is_rspec_string( requestString, None, None, logger=self.logger ):
            # This line seems to insert extra \ns - GCF ticket #202
#            content = rspec_util.getPrettyRSpec(requestString)
            content = string.replace(requestString, "\\n", '\n')
        else:
            content = "<!-- No valid RSpec returned. -->"
            if requestString is not None:
                content += "\n<!-- \n" + requestString + "\n -->"
        rspecfileName = _construct_output_filename(opts, slicename, self.url, self.urn, opName + '-request', '.xml', 1)
        # Set -o to ensure this goes to a file, not logger or stdout
        opts_copy = copy.deepcopy(opts)
        opts_copy.output = True
        _printResults(opts_copy, self.logger, header, content, rspecfileName)
        self.logger.info("Saved AM %s new request RSpec to file %s", self.urn, rspecfileName)
#        with open (rspecfileName, 'w') as file:
#            file.write(self.requestDom.toprettyxml())

        # Set opts.raiseErrorOnV2AMAPIError so we can see the error codes and respond directly
        omniargs = ['-o', '--raiseErrorOnV2AMAPIError', '-a', self.url, opName, slicename, rspecfileName]
        self.logger.info("\nDoing %s at %s", opName, self.url)
        self.logger.debug("omniargs %r", omniargs)

        result = None
        success = False

        # If fakeMode read results from a file
        if opts.fakeModeDir:
            self.logger.info("Doing FAKE allocation")

            # FIXME: Take the expanded request from the SCS and pretend it is the manifest
            # That way, we get the VLAN we asked for
            resultPath = "./stitching-scs-expanded-request.xml"

#            # For now, results file only has a manifest. No JSON
#            resultFileName = _construct_output_filename(opts, slicename, self.url, self.urn, opName+'-result', '.json', 1)
#            resultPath = os.path.join(opts.fakeModeDir, resultFileName)
#            if not os.path.exists(resultPath):
#                resultFileName = _construct_output_filename(opts, slicename, self.url, self.urn, opName+'-result', '.xml', 1)
#                resultPath = os.path.join(opts.fakeModeDir, resultFileName)
#                if not os.path.exists(resultPath):
#                    self.logger.error("Fake results file %s doesn't exist", resultPath)
#                    resultPath = None

            if resultPath:
                self.logger.info("Reading reserve results from %s", resultPath)
                try:
                    with open(resultPath, 'r') as file:
                        resultsString = file.read()
                    try:
                        result = json.loads(resultsString, encoding='ascii')
                    except Exception, e2:
                        self.logger.debug("Failed to read fake results as json: %s", e2)
                        result = resultsString
                        success = True
                except Exception, e:
                    self.logger.error("Failed to read result string from %s: %s", resultPath, e)

            if not result:
                # Fallback fake mode behavior
                success = True
                time.sleep(random.randrange(1, 6))
                for hop in self.hops:
                    hop._hop_link.vlan_suggested_manifest = hop._hop_link.vlan_suggested_request
                    hop._hop_link.vlan_range_manifest = hop._hop_link.vlan_range_request
        else:
            try:
                # FIXME: Threading!
                # FIXME: AM API call timeout!
                # FIXME: Turn down omni logging using logging.disable?
                # FIXME: Do we do something with log level or log format or file for omni calls?
                (text, result) = omni.call(omniargs, opts)
                success = True
            except AMAPIError, e:
                self.logger.error("Failed with AMAPI Error trying to reserve at %s: %s", self.url, e)

                # FIXME: handle BUSY
                result = e.returnStruct
            except Exception, e:
                self.logger.error("Failed to reserve at %s: %s", self.url, e)
                # FIXME: call self.handleAllocateError
                raise StitchingError(e)
        return result, success

    def copyVLANsAndDetectRedo(self):
        '''Copy VLANs to this AMs hops from previous manifests. Check if we already had manifests.
        If so, but the inputs are incompatible, then mark this to be deleted. If so, but the
        inputs are compatible, then an AM upstream was redone, but this is alreadydone.'''

        hadPreviousManifest = self.manifestDom != None
        mustDelete = False # Do we have old reservation to delete?
        alreadyDone = hadPreviousManifest # Did we already complete this AM? (and this is just a recheck)
        for hop in self.hops:
            if not hop.import_vlans:
                if not hop._hop_link.vlan_suggested_manifest:
                    alreadyDone = False
                continue

            # Calculate the new suggested/avail for this hop
            if not hop.import_vlans_from:
                self.logger.warn("Hop %s imports vlans but has no import from?", hop)
                continue

            new_suggested = hop._hop_link.vlan_suggested_request or VLANRange.fromString("any")
            if hop.import_vlans_from._hop_link.vlan_suggested_manifest:
                # FIXME: Need deep copy?
                new_suggested = hop.import_vlans_from._hop_link.vlan_suggested_manifest.copy()
            else:
                self.logger.warn("Hop %s's import_from %s had no suggestedVLAN manifest", hop, hop.import_vlans_from)

            # If we've noted VLANs we already tried that failed (cause of later failures
            # or cause the AM wouldn't give the tag), then be sure to exclude those
            # from new_suggested - that is, if new_suggested would be in that set, then we have
            # an error - gracefully exit, either to SCS excluding this hop or to user
            if new_suggested <= hop.vlans_unavailable:
                # FIXME
                raise StitchingError("%s picked new_suggested %s that is in the set of VLANs that we know won't work: %s", hop, new_suggested, hop.vlans_unavailable)

            int1 = VLANRange.fromString("any")
            int2 = VLANRange.fromString("any")
            if hop.import_vlans_from._hop_link.vlan_range_manifest:
                int1 = hop.import_vlans_from._hop_link.vlan_range_manifest
            else:
                self.logger.warn("Hop %s's import_from %s had no avail VLAN manifest", hop, hop.import_vlans_from)
            if hop._hop_link.vlan_range_request:
                int2 = hop._hop_link.vlan_range_request
            else:
                self.logger.warn("Hop %s had no avail VLAN request", hop, hop.import_vlans_from)
            new_avail = int1 & int2

            # If we've noted VLANs we already tried that failed (cause of later failures
            # or cause the AM wouldn't give the tag), then be sure to exclude those
            # from new_avail. And if new_avail is now empty, that is
            # an error - gracefully exit, either to SCS excluding this hop or to user
            new_avail2 = new_avail - hop.vlans_unavailable
            if new_avail2 != new_avail:
                self.logger.debug("%s computed vlanRange %s smaller due to excluding known unavailable VLANs. Was otherwise %s", hop, new_avail2, new_avail)
                new_avail = new_avail2
            if len(new_avail) == 0:
                # FIXME
                raise StitchingError("%s computed vlanRange is empty", hop)

            if not new_suggested <= new_avail:
                # We're somehow asking for something not in the avail range we're asking for.
                # An error
                self.logger.warn("Hop %s Calculated suggested %s not in available range %s", hop, new_suggested, new_avail)
                raise StitchingError("Hop %s could not be processed: calculated a suggested VLAN of %s that is not in the calculated availabel range %s", hop, new_suggested, new_avail)

            # If we have a previous manifest, we might be done or might need to delete a previous reservation
            if hop._hop_link.vlan_suggested_manifest:
                if not hadPreviousManifest:
                    raise StitchingError("AM %s had no previous manifest, but its hop %s did", self, hop)
                if hop._hop_link.vlan_suggested_request != new_suggested:
                    # If we already have a result but used different input, then this result is suspect. Redo.
                    self.logger.warn("AM %s had previous manifest and used different suggested VLAN for hop %s (old request %s != new request %s)", self, hop, hop._hop_link.vlan_suggested_request, new_suggested)
                    hop._hop_link.vlan_suggested_request = new_suggested
                    # if however the previous suggested_manifest == new_suggested, then maybe this is OK?
                    if hop._hop_link.vlan_suggested_manifest == new_suggested:
                        self.logger.info("But hop %s VLAN suggested manifest is the new request, so leave it alone", hop)
                    else:
                        mustDelete = True
                        alreadyDone = False
                else:
                    self.logger.info("AM %s had previous manifest and used same suggested VLAN for hop %s (%s)", self, hop, hop._hop_link.vlan_suggested_request)
                    # So for this hop at least, we don't need to redo this AM
            else:
                alreadyDone = False
                # No previous result
                if hadPreviousManifest:
                    raise StitchingError("AM %s had a previous manifest but hop %s did not", self, hop)
                if hop._hop_link.vlan_suggested_request != new_suggested:
                    self.logger.debug("Hop %s changing VLAN suggested from %s to %s", hop, hop._hop_link.vlan_suggested_request, new_suggested)
                    hop._hop_link.vlan_suggested_request = new_suggested
                else:
                    self.logger.debug("Hop %s already had VLAN suggested %s", hop, hop._hop_link.vlan_suggested_request)

            # Now check the avail range as we did for suggested
            if hop._hop_link.vlan_range_manifest:
                if not hadPreviousManifest:
                    self.logger.error("AM %s had no previous manifest, but its hop %s did", self, hop)
                if hop._hop_link.vlan_range_request != new_avail:
                    # If we already have a result but used different input, then this result is suspect. Redo?
                    self.logger.warn("AM %s had previous manifest and used different avail VLAN range for hop %s (old request %s != new request %s)", self, hop, hop._hop_link.vlan_range_request, new_avail)
                    if hop._hop_link.vlan_suggested_manifest and hop._hop_link.vlan_suggested_manifest not in new_avail:
                        # new avail doesn't contain the previous manifest suggested. So new avail would have precluded
                        # using the suggested we picked before. So we have to redo
                        mustDelete = True
                        alreadyDone = False
                        self.logger.warn("Hop %s previous manifest suggested %s not in new avail %s - redo this AM", hop, hop._hop_link.vlan_suggested_manifest, new_avail)
                    else:
                        # what we picked before still works, so leave it alone
                        self.logger.debug("Hop %s had avail range manifest %s, and previous avail range request (%s) != new (%s), but previous suggested manifest %s is in the new avail range, so it is still good", hop, hop._hop_link.vlan_range_manifest, hop._hop_link.vlan_range_request, new_avail, hop._hop_link.vlan_suggested_manifest)

                    # Either way, record what we want the new request to be, so later if we redo we use the right thing
                    hop._hop_link.vlan_range_request = new_avail
                else:
                    self.logger.info("AM %s had previous manifest and used same avail VLAN range for hop %s (%s)", self, hop, hop._hop_link.vlan_range_request)
            else:
                alreadydone = False
                # No previous result
                if hadPreviousManifest:
                    raise StitchingError("AM %s had a previous manifest but hop %s did not", self, hop)
                if hop._hop_link.vlan_range_request != new_avail:
                    self.logger.debug("Hop %s changing avail VLAN from %s to %s", hop, hop._hop_link.vlan_range_request, new_avail)
                    hop._hop_link.vlan_range_request = new_avail
                else:
                    self.logger.debug("Hop %s already had avail VLAN %s", hop, hop._hop_link.vlan_range_request)
        # End of loop over hops to copy VLAN tags over and see if this is a redo or we need to delete
        return mustDelete, alreadyDone

    def deleteReservation(self, opts, slicename):
        '''Delete any previous reservation/manifest at this AM'''
        self.completed = False
        
        # Clear old manifests
        self.manifestDom = None
        for hop in self.hops:
            hop._hop_link.vlan_suggested_manifest = None
            hop._hop_link.vlan_range_manifest = None

        # Now mark all AMs that depend on this AM as incomplete, so we'll try them again
        # FIXME: This makes everything in chain get redone. Could we mark only the immediate
        # children, so only if those get deleted do their children get marked? Note the cost
        # isn't so high - it means falling into this code block and doing the above logic
        # that discovers existing manifests
        for agg in self.isDependencyFor:
            agg.completed = False

        # FIXME: Set a flag marking it is being deleted? Set inProcess?

        # Delete the previous reservation
        # FIXME: Do we do something with log level or log format or file for omni calls?
        # FIXME: Supply --raiseErrorOnAMAPIV2Error?
        opName = 'deletesliver'
        if self.api_version > 2:
            opName = 'delete'
        omniargs = ['-a', self.url, opName+'sliver', slicename]
        self.logger.info("Doing %s at %s", opName, self.url)
        if not opts.fakeModeDir:
            try:
                (text, (successList, fail)) = omni.call(omniargs, opts)
                if not self.url in successList:
                    raise StitchingError("Failed to delete prior reservation at %s: %s", self.url, text)
                else:
                    self.logger.debug("Result: %s", text)
            except OmniError, e:
                self.logger.error("Failed to %s: %s", opName, e)
                raise StitchingError(e)
        # FIXME: Fake mode delete results from a file?

        # FIXME: Set a flag marking this AM was deleted?
        return

class Hop(object):
    # A hop on a path in the stitching element
    # Note this is path specific (and has a path reference)

    # XML tag constants
    ID_TAG = 'id'
    TYPE_TAG = 'type'
    LINK_TAG = 'link'
    NEXT_HOP_TAG = 'nextHop'

    @classmethod
    def fromDOM(cls, element):
        """Parse a stitching hop from a DOM element."""
        # FIXME: getAttributeNS?
        id = element.getAttribute(cls.ID_TAG)
        isLoose = False
        if element.hasAttribute(cls.TYPE_TAG):
            hopType = element.getAttribute(cls.TYPE_TAG)
            if hopType.lower().strip() == 'loose':
                isLoose = True
        hop_link = None
        next_hop = None
        for child in element.childNodes:
            if child.localName == cls.LINK_TAG:
                hop_link = HopLink.fromDOM(child)
            elif child.localName == cls.NEXT_HOP_TAG:
                next_hop = child.firstChild.nodeValue
                if next_hop == 'null':
                    next_hop = None
        hop = Hop(id, hop_link, next_hop)
        if isLoose:
            hop.loose = True
        return hop

    def __init__(self, id, hop_link, next_hop):
        self._id = id
        self._hop_link = hop_link
        self._next_hop = next_hop
        self._path = None
        self._aggregate = None
        self._import_vlans = False
        self._dependencies = []
        self.idx = None
        self.logger = logging.getLogger('stitch.Hop')
        self.import_vlans_from = None # a pointer to another hop

        # If True, then next request to SCS should explicitly
        # mark this hop as loose
        self.loose = False
        # Set to true so later call to SCS will explicitly exclude this Hop
        self.excludeFromSCS = False

        # VLANs we know are not possible here - cause of VLAN_UNAVAILABLE
        # or cause a suggested was not picked.
        # Use this to avoid picking these later
        self.vlans_unavailable = VLANRange()

    def __str__(self):
        return "<Hop %r on path %r>" % (self.urn, self._path.id)

    @property
    def urn(self):
        return self._hop_link and self._hop_link.urn

    @property
    def aggregate(self):
        return self._aggregate

    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, path):
        self._path = path

    @aggregate.setter
    def aggregate(self, agg):
        self._aggregate = agg

    @property
    def import_vlans(self):
        return self._import_vlans

    @import_vlans.setter
    def import_vlans(self, value):
        self._import_vlans = value

    @property
    def dependsOn(self):
        return self._dependencies

    def add_dependency(self, hop):
        self._dependencies.append(hop)

    def editChangesIntoDom(self, domHopNode):
        '''Edit any changes made in this element into the given DomNode'''
        # Note the parent RSpec object's dom is not touched, unless the given node is from that document
        # Here we just like the HopLink do its thing

        # Incoming node should be the node for this hop
        nodeId = domHopNode.getAttribute(self.ID_TAG)
        if nodeId != self._id:
            raise StitchingError("Hop %s given Dom node with different Id: %s" % (self, nodeId))

        # Mark hop explicitly loose if necessary
        if self.loose:
            domHopNode.setAttribute(self.TYPE_TAG, 'loose')

        for child in domHopNode.childNodes:
            if child.localName == self.LINK_TAG:
                self.logger.debug("Hop %s editChanges calling _hop_link with node %r", self, child)
                self._hop_link.editChangesIntoDom(child)

class RSpec(GENIObject):
    '''RSpec'''
    __simpleProps__ = [ ['stitching', Stitching] ]

    def __init__(self, stitching=None): 
        super(RSpec, self).__init__()
        self.stitching = stitching
        self._nodes = []
        self._links = [] # Main body links
        # DOM used to construct this: edits to objects are not reflected here
        self.dom = None
        # Note these are not Aggregate objects to avoid any loops
        self.amURNs = set() # AMs mentioned in the RSpec

    @property
    def nodes(self):
        return self._nodes

    @nodes.setter
    def nodes(self, nodeList):
        self._setListProp('nodes', nodeList, Node)

    @property
    def links(self):
        # Gets main body link elements
        return self._links

    @links.setter
    def links(self, linkList):
        self._setListProp('links', linkList, Link)

    def find_path(self, link_id):
        """Find the stitching path with the given id and return it. If no path
        matches the given id, return None.
        """
        return self.stitching and self.stitching.find_path(link_id)

    def find_link(self, hop_urn):
        """Find the main body link with the given id and return it. If no link
        matches the given id, return None.
        """
        for link in self._links:
            if link.id == link_id:
                return link
        return None


class Node(GENIObject):
    CLIENT_ID_TAG = 'client_id'
    COMPONENT_MANAGER_ID_TAG = 'component_manager_id'
    @classmethod
    def fromDOM(cls, element):
        """Parse a Node from a DOM element."""
        # FIXME: getAttributeNS?
        client_id = element.getAttribute(cls.CLIENT_ID_TAG)
        amID = element.getAttribute(cls.COMPONENT_MANAGER_ID_TAG)
        return Node(client_id, amID)

    def __init__(self, client_id, amID):
        super(Node, self).__init__()
        self.id = client_id
        self.amURN = amID

class Link(GENIObject):
    # A link from the main body of the rspec
    # Note the link client_id matches the hop_urn from the workflow matches the HopLink ID

    __ID__ = validateTextLike
    __simpleProps__ = [ ['client_id', str]]

    # XML tag constants
    CLIENT_ID_TAG = 'client_id'
    COMPONENT_MANAGER_TAG = 'component_manager'
    INTERFACE_REF_TAG = 'interface_ref'
    NAME_TAG = 'name'
    SHARED_VLAN_TAG = 'link_shared_vlan'

    @classmethod
    def fromDOM(cls, element):
        """Parse a Link from a DOM element."""
        # FIXME: getAttributeNS?
        client_id = element.getAttribute(cls.CLIENT_ID_TAG)
        refs = []
        aggs = []
        hasSharedVlan = False
        for child in element.childNodes:
            if child.localName == cls.COMPONENT_MANAGER_TAG:
                name = child.getAttribute(cls.NAME_TAG)
                agg = Aggregate.find(name)
                aggs.append(agg)
            elif child.localName == cls.INTERFACE_REF_TAG:
                # FIXME: getAttributeNS?
                c_id = child.getAttribute(cls.CLIENT_ID_TAG)
                ir = InterfaceRef(c_id)
                refs.append(ir)
            # If the link has the shared_vlan extension, note this - not a stitching reason
            elif child.localName == cls.SHARED_VLAN_TAG:
#                print 'got shared vlan'
                hasSharedVlan = True
        link = Link(client_id)
        link.aggregates = aggs
        link.interfaces = refs
        link.hasSharedVlan = hasSharedVlan
        return link

    def __init__(self, client_id):
        super(Link, self).__init__()
        self.id = client_id
        self._aggregates = []
        self._interfaces = []
        self.hasSharedVlan = False

    @property
    def interfaces(self):
        return self._interfaces

    @interfaces.setter
    def interfaces(self, interfaceList):
        self._setListProp('interfaces', interfaceList, InterfaceRef)

    @property
    def aggregates(self):
        return self._aggregates

    @aggregates.setter
    def aggregates(self, aggregateList):
        self._setListProp('aggregates', aggregateList, Aggregate)


class InterfaceRef(object):
     def __init__(self, client_id):
         self.client_id = client_id


class HopLink(object):
    # From the stitching element, the link on the hop on a path
    # Note this is Path specific

    # XML tag constants
    ID_TAG = 'id'
    HOP_TAG = 'hop'
    VLAN_TRANSLATION_TAG = 'vlanTranslation'
    VLAN_RANGE_TAG = 'vlanRangeAvailability'
    VLAN_SUGGESTED_TAG = 'suggestedVLANRange'

    @classmethod
    def fromDOM(cls, element):
        """Parse a stitching path from a DOM element."""
        # FIXME: getAttributeNS?
        id = element.getAttribute(cls.ID_TAG)
        # FIXME: getElementsByTagNameNS?
        vlan_xlate = element.getElementsByTagName(cls.VLAN_TRANSLATION_TAG)
        if vlan_xlate:
            # If no firstChild or no nodeValue, assume false
            if len(vlan_xlate) > 0 and vlan_xlate[0].firstChild:
                x = vlan_xlate[0].firstChild.nodeValue
            else:
                x = 'False'
            vlan_translate = x.lower() in ('true')
        vlan_range = element.getElementsByTagName(cls.VLAN_RANGE_TAG)
        if vlan_range:
            # vlan_range may have no child or no nodeValue. Meaning would then be 'any'
            if len(vlan_range) > 0 and vlan_range[0].firstChild:
                vlan_range_value = vlan_range[0].firstChild.nodeValue
            else:
                vlan_range_value = "any"
            vlan_range_obj = VLANRange.fromString(vlan_range_value)
        else:
            vlan_range_obj = VLANRange()            
        vlan_suggested = element.getElementsByTagName(cls.VLAN_SUGGESTED_TAG)
        if vlan_suggested:
            # vlan_suggested may have no child or no nodeValue. Meaning would then be 'any'
            if len(vlan_suggested) > 0 and vlan_suggested[0].firstChild:
                vlan_suggested_value = vlan_suggested[0].firstChild.nodeValue
            else:
                vlan_suggested_value = "any"                
            vlan_suggested_obj = VLANRange.fromString(vlan_suggested_value)
        else:
            vlan_suggested_obj = VLANRange()            
        hoplink = HopLink(id)
        hoplink.vlan_xlate = vlan_translate
        hoplink.vlan_range_request = vlan_range_obj
        hoplink.vlan_suggested_request = vlan_suggested_obj
        return hoplink

    def __init__(self, urn):
        self.urn = urn
        self.vlan_xlate = False
        self.vlan_range_request = ""
        self.vlan_suggested_request = None
        self.vlan_range_manifest = ""
        self.vlan_suggested_manifest = None
        self.logger = logging.getLogger('stitch.HopLink')

    def editChangesIntoDom(self, domNode, request=True):
        '''Edit any changes made in this element into the given DomNode'''
        # Note that the parent RSpec object's dom is not touched, unless this domNode is from that
        # Here we edit in the new vlan_range and vlan_available
        # If request is False, use the manifest values. Otherwise, use requested.

        # Incoming node should be the node for this hop
        nodeId = domNode.getAttribute(self.ID_TAG)
        if nodeId != self.urn:
            raise StitchingError("Hop Link %s given Dom node with different Id: %s" % (self, nodeId))

        if request:
            newVlanRangeString = str(self.vlan_range_request)
            newVlanSuggestedString = str(self.vlan_suggested_request)
        else:
            newVlanRangeString = str(self.vlan_range_manifest)
            newVlanSuggestedString = str(self.vlan_suggested_manifest)

        vlan_range = domNode.getElementsByTagName(self.VLAN_RANGE_TAG)
        if vlan_range and len(vlan_range) > 0:
            # vlan_range may have no child or no nodeValue. Meaning would then be 'any'
            if vlan_range[0].firstChild:
                # Set the value
                vlan_range[0].firstChild.nodeValue = newVlanRangeString
                self.logger.debug("Set vlan range on node %r: %s", vlan_range[0], vlan_range[0].firstChild.nodeValue)
            else:
                vlan_range[0].appendChild(Document.createTextNode(newVlanRangeString))
        else:
            vlanRangeNode = Document.createElement(self.VLAN_RANGE_TAG)
            vlanRangeNode.appendChild(Document.createTextNode(newVlanRangeString))
            # Find the switchingCapabilitySpecificInfo_L2sc node and append it there
            l2scNodes = domNode.getElementsByTagName('switchingCapabilitySpecificInfo_L2sc')
            if l2scNodes and len(l2scNodes) > 0:
                l2scNodes[0].appendChild(vlanRangeNode)

        vlan_suggested = domNode.getElementsByTagName(self.VLAN_SUGGESTED_TAG)
        if vlan_suggested and len(vlan_suggested) > 0:
            # vlan_suggested may have no child or no nodeValue. Meaning would then be 'any'
            if vlan_suggested[0].firstChild:
                # Set the value
                vlan_suggested[0].firstChild.nodeValue = newVlanSuggestedString
                self.logger.debug("Set vlan suggested on node %r: %s", vlan_suggested[0], vlan_suggested[0].firstChild.nodeValue)
            else:
                vlan_suggested[0].appendChild(Document.createTextNode(newVlanSuggestedString))
        else:
            vlanSuggestedNode = Document.createElement(self.VLAN_RANGE_TAG)
            vlanSuggestedNode.appendChild(Document.createTextNode(newVlanSuggestedString))
            # Find the switchingCapabilitySpecificInfo_L2sc node and append it there
            l2scNodes = domNode.getElementsByTagName('switchingCapabilitySpecificInfo_L2sc')
            if l2scNodes and len(l2scNodes) > 0:
                l2scNodes[0].appendChild(vlanSuggestedNode)
